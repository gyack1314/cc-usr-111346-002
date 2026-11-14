# 研究影像DICOMweb的逐帧身份与批量正文关联

本项目保存研究协作采集所需的本地资料，提供内容摘要、记录身份与登记关系检查。当前读取入口只返回表数量及身份检查结果，不解释原始格式或获取远端资料。

资料表包含instances、requested。字段单位和关系见 docs/data-contract.md。data/normal.json登记常规资料，data/boundary.json登记特殊业务情形，data/broken-digest.json用于检查资料损坏的拒绝行为。JSONL文件按清单内SHA256和字节长度绑定。sources/含合成原始资料及独立构造的原值记录，sources/manifest.json登记其长度及摘要。当前只核验这些文件的完整性，不解析其业务格式。

运行环境为 Python 3.11及以上，当前仅使用标准库，无需额外服务。包内io.py负责资料读取，domain.py负责表集合与引用检查，cli.py提供本地检查入口。

```sh
python -m unittest discover -s tests -v
python -m compileall -q dicom_data tests
python -m dicom_data.cli data/normal.json
python -m dicom_data.cli data/boundary.json
```

## DICOMweb逐帧采集端 dicomweb_collector

在本地资料检查之外，新增受授权研究影像接口的逐帧采集端：只收指定帧，把研究、
序列、实例、帧的身份从请求一直保留到落盘，不分析影像内容。设计约定见
docs/acquisition-design.md。

- 限定未压缩Explicit VR Little Endian、BitsAllocated 8/16、单样本灰度的原始帧；
  元数据按DICOM JSON解析，VR、Value、InlineBinary、BulkDataURI分开处理，
  空Value、零长度值与标签缺失分别留存。
- 按真正的MIME边界行流式读取部件；部件身份优先按Content-Location关联规范帧
  地址，无可靠顺序依据时拒绝歧义；同帧重复部件字节不同即冲突，缺帧不以空文件
  顶替。
- 元数据、帧、缓存必须属于同一冻结代（generation+强ETag）；无法验证版本绑定
  的来源只标一致性未知。中断恢复只复用已核验且版本相符的帧。
- 独立帧清单检查器复核落盘结果，运行报告记录每次请求与字节统计、每帧来自
  哪一个响应部件。

```sh
python -m unittest discover -s tests -v
python -m compileall -q dicomweb_collector tests
python -m dicomweb_collector.check <store目录>
```

容器内使用同样的检查入口：

```sh
docker build -t dicom-data .
docker run --rm dicom-data
```
