# ASR v1.2 公共代理语料来源与完整性清单

**核验时间：** 2026-08-25（Asia/Shanghai）  
**状态：** 来源获取与归档校验完成；确定性候选提取、实体标签人工复核和 blind 冻结尚未完成。

## 结论

采用 AliMeeting Eval、ASCEND 与 HI-MIA-CW 的最小组合，不再下载 MagicData-RAMC 与 MUSAN。当前下载量约 4.97 GiB，已覆盖会议/近讲/室内噪声、普通话-英语 code-switch、香港地区口音和中文混淆词负样本；与原组合相比减少约 26 GiB 下载，并避开 MagicData 的 `CC BY-NC-ND 4.0` 限制。

这些数据作为“本项目用途相近的公共代理 blind”，不是本产品用户真实录音。若最终模型差异的 CI 跨越门槛，结论必须降级为 `Experimental`，并补充已授权的真实产品域语料，不能把公共代理结果夸大为生产域确定性结论。本轮未扫描或读取个人录音目录。

## 来源、许可与制品

| 来源 | 固定身份与许可 | 本机制品 | SHA-256 / 完整性 |
|:---|:---|:---|:---|
| AliMeeting Eval | [OpenSLR SLR119](https://www.openslr.org/119/)，`CC BY-SA 4.0` | `Eval_Ali.tar.gz`，3,673,718,355 bytes | `dc47343b2474b5ebcf458927e878155f6ddeb59c85e685b3645c32a1f9578d92`；73 members / 66 files / 0 unsafe path |
| ASCEND | 发布方指向的 [CAiRE/ASCEND](https://huggingface.co/datasets/CAiRE/ASCEND)，revision `b65b9bb87a0412eb94a659660819060825e74b9f`，`CC BY-SA 4.0` | 5 个 parquet，1,223,536,062 bytes | 五个文件 hash 见下表；schema、行数、时长、speaker/session 已实读 |
| HI-MIA-CW | [OpenSLR SLR120](https://www.openslr.org/120/)，`CC BY-SA 4.0` | `data.tgz` 550,623,081 bytes；`resource.tgz` 55,193 bytes | `5de169ac…f83c91a` / `8628c75e…1ee4e4`；16,344 + 2 members / 0 unsafe path |

ModelScope 优先策略已实测执行：

- `OpenDataLab/ASCEND@master` 只有 `README.md`、`metafile.yaml`、`dataset_infos.json` 三个元数据文件，没有音频和逐字稿，因此回退到发布方指向的 Hugging Face 固定 revision。
- `modelscope/AliMeeting` 只有约 655KB manifest/说明，没有音频；并且卡片标注 Apache 2.0，与上游 OpenSLR 的 `CC BY-SA 4.0` 冲突。本项目一律以上游许可和音频为准。
- HI-MIA-CW 没有可确认的 ModelScope 完整镜像，直接使用 OpenSLR。中国镜像 TLS 证书过期后未绕过校验，改用 OpenSLR 页面列出的 EU HTTPS 镜像。

### ASCEND parquet hash

| 文件 | bytes | SHA-256 |
|:---|---:|:---|
| `test-00000-of-00001.parquet` | 105,756,434 | `a4c81d2b5ed6124f052089a695972808c16e0ce0c365ec9773c5d1a8fcf043a7` |
| `train-00000-of-00003.parquet` | 316,735,328 | `3d66ba76f324e0711b779cfb01ee4e772a24a929e1d77a2063cde0506f75976f` |
| `train-00001-of-00003.parquet` | 366,824,932 | `569f84f771c3637ca8535bd35e10e62feed2c240833534711909a9c04f51e589` |
| `train-00002-of-00003.parquet` | 327,687,102 | `aa76b7ef4a74ff111fd1d2573d0b69e6b6f901df6054618d8e5658a7a394523e` |
| `validation-00000-of-00001.parquet` | 106,532,266 | `3bdec53d2abfd3dd4f0d86a6df4e27e60f20660edc9b66055ae0ef8ec05cf7e2` |

## 实测候选池

### AliMeeting Eval

8 个 meeting session、25 个互不重复 speaker；近场为单通道，远场为 8 通道 16kHz WAV。总音频约 4 小时，单场 26.2–37.3 分钟；按 TextGrid 合并后的有效语音约 233 分钟，session overlap 比例约 7.2%–57.4%。

预分配保持完整 session/speaker 隔离：

| Split | Sessions | Speakers | 主要用途 |
|:---|:---|---:|:---|
| Dev | `R8007_M8010` | 4 | 高重叠校准，不进入 blind |
| Core | `R8001_M8004`、`R8003_M8001`、`R8008_M8013` | 11 | near-field、meeting、noise、entity |
| Reserve | `R8007_M8011`、`R8009_M8018`、`R8009_M8019`、`R8009_M8020` | 10 | near-field、meeting、noise、entity |

远场抽样固定使用同一阵列通道，不对 8 通道做平均混音；Stage 1 CER 只采用无跨 speaker overlap 的标注区间。含 overlap 的连续会议留给 Stage 2/3 流式与系统链路，不混入单一文本 CER 主指标。

### ASCEND

| Split | Rows | 实测时长 | Speakers | 用途 |
|:---|---:|---:|---:|:---|
| train | 9,869 | 31,589,400ms | 18 | Dev / Public 候选池 |
| validation | 1,130 | 3,323,603ms | 3 | Reserve code-switch / accent |
| test | 1,315 | 3,302,971ms | 2 | Core code-switch / accent |

三个 split 的 speaker 集合实测互斥。`session_id` 数字在不同 split 中重复，因此冻结 ID 必须使用 `ascend:<split>:<session_id>` 命名空间，禁止按裸整数判断 session 隔离。

### HI-MIA-CW

16,343 条 16kHz WAV，35 个 speaker，均有逐文件 transcription；内容是“Hi, Mia”中文混淆词，适合作为 non-target speech negative。Core、Reserve、Dev 必须使用不相交 speaker 集，且负样本只检验误触发/幻觉，不进入正文本 CER 分母。

## 冻结前验收条件

- [x] 所有原始归档/固定 parquet 位于项目外目录。
- [x] 原始制品大小、SHA-256、归档安全与关键 schema 已验证。
- [x] Core/Reserve 可分配到互斥 AliMeeting session/speaker；ASCEND split speaker 互斥。
- [x] 三个 blind 来源许可均按上游 `CC BY-SA 4.0` 记录。
- [ ] 采用固定 seed `asr-v1.2-20260825` 生成确定性候选顺序。
- [ ] 主场景配额严格等于 Core 9/17/9/11/6/6/2 分钟和 Reserve 6/13/6/9/4/4/3 分钟。
- [ ] AliMeeting entity 标签逐条人工复核，不能仅凭普通中文文本自动推断。
- [ ] Core/Reserve reference、manifest、cluster 与 `analysis-plan.json` 在任何 blind 输出产生前同时封存。
