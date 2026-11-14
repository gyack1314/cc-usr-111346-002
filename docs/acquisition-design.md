# DICOMweb逐帧采集端设计说明

`dicomweb_collector` 从受授权的研究影像接口只收指定帧，把研究、序列、实例、帧的
身份从请求一直保留到落盘。不分析影像内容，不实现压缩编解码，不做完整
DICOM Part10解析。

## 针对的历史事故

1. **把multipart第一个部件当第一帧**：服务端调换部件顺序后样本切片对应错。
   现在部件身份只认Content-Location解析出的规范帧地址；顺序仅当请求本身
   枚举了有序帧列表且来源契约声明按序返回时才可作依据；两者矛盾或都没有
   时拒绝，不猜。
2. **元数据是新版、像素取自旧缓存**：现在元数据、帧、批量正文、缓存都必须
   属于同一冻结代（源generation + 实例元数据强ETag）。代不符即拒绝；
   没有可验证版本绑定的来源只标"一致性未知"，不宣称已取得同版完整资料。

## 三段接口

```
来源适配器 SourceAdapter.fetch(url, headers) -> SourceResponse
        │  （DICOM JSON元数据 / WADO-RS multipart / BulkData）
        ▼
元数据目录 MetadataCatalog  ──冻结──▶  帧请求规划 FrameRequestPlanner
        │  CatalogEntry{descriptor,      │  FrameRequest{request_id,
        │  version, metadata_url}        │  instance, frame_numbers, url}
        ▼                                ▼
        └──────────▶  部件接收 PartReceiver.consume()  ◀──────────┘
                        │  逐部件：身份判定 + 布局/长度/语法核对
                        ▼
                     FrameStore.commit_part() → 清单 → register_instance()
```

- **元数据目录**（`dicomjson.py`）：解析DICOM JSON，VR、Value、InlineBinary、
  BulkDataURI分开处理；标签缺失、空Value、零长度值分别留存。Rows、Columns、
  BitsAllocated、SamplesPerPixel、NumberOfFrames及每帧字节数在此冻结，
  采集期间不可变。实例身份取自请求，元数据内UID只做交叉核对。
- **帧请求规划**（`planner.py`）：把"还缺哪些帧"切成HTTP请求。合并/拆分只影响
  请求边界，不影响帧身份——身份在接收端按部件重新确定。
- **部件接收**（`receiver.py` + `mime.py`）：流式解析multipart，逐部件判定身份、
  核对transfer-syntax、声明长度、像素布局，核验后交FrameStore落盘。

## 不变量

- **身份链**：FrameKey(study, series, sop, 帧号) 从请求URL派生，出现在
  请求规划、部件判定、落盘路径 `frames/<study>/<series>/<sop>/<NNNNNN>.raw`
  和清单记录里，全程同一值。
- **MIME边界**：只认真正的边界行（CRLF + `--boundary` + 合法尾巴）。边界字符串
  出现在二进制中间、或行首但后续字节不合法，一律视为正文，不截断。
  缺少关闭定界符按截断拒绝。
- **部件身份**：Content-Location优先；请求顺序模式仅当来源声明
  `ordered_frame_parts`；两者都有必须一致；都没有抛AmbiguousPartError。
  非请求实例或范围外帧的部件抛UnexpectedPartError，不得混入。
- **帧完整性**：同帧重复部件字节相同去重、不同即FrameConflictError；
  缺帧不以空文件顶替，所需帧未全部核验不得登记实例
  （IncompleteInstanceError）。
- **像素布局**：帧长度必须等于冻结的像素字节数，或（奇数像素时）等于其
  偶数填充长度；填充不算像素，落盘只存像素字节并记录填充数。
- **版本绑定**：VersionTag = (generation, 元数据强ETag)。响应代与目录代不符
  抛VersionMismatchError；缺generation或强ETag时继续采集但标一致性未知。
  恢复只复用已核验且版本相符的帧；tmp/里的半个部件一律丢弃，不续拼。
- **范围与凭据**：BulkDataURI可相对元数据响应地址，逐跳（含重定向链）解析，
  每一跳都必须在配置的研究源范围内；会话凭据只发同源，重定向把请求带出
  同源即拒绝。
- **内存**：解析器只保留当前块加边界进位；部件正文写临时对象
  （SpooledTemporaryFile，超出预算落tmp/），核验后才改名进入frames/。

## 限定

- 仅未压缩Explicit VR Little Endian（1.2.840.10008.1.2.1），BitsAllocated
  为8或16，单样本灰度。元数据级（X-Transfer-Syntax）、multipart级、部件级
  的transfer-syntax三处一起核对。
- PhotometricInterpretation存在时必须为MONOCHROME1/2；SamplesPerPixel缺失
  或为空时按单样本默认并记入descriptor.assumed，不静默。
- 部件必须至少携带一个头部（受控来源恒有Content-Type）；零头部部件拒绝。

## 独立检查与统计

- `python -m dicomweb_collector.check <store目录>`：不信任采集代码，按
  catalog.json / run_spec.json / manifest.jsonl 重算每帧SHA256、核对版本
  绑定、长度与出处字段，退出码非零即不完整。
- RunReport与落盘的run_report.json记录每次请求的URL、状态、字节数、代、
  ETag、是否带凭据，以及每帧来自哪一个响应部件
  （`rq-NNNN#partM` 或 `bulk:<url>@<offset>`）。
