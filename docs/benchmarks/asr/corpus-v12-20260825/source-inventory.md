# ASR v1.2 公共代理语料来源与完整性清单

**核验时间：** 2026-08-25（Asia/Shanghai）  
**状态：** 公共代理候选提取与 105 分钟封存已完成；正式目标域 blind、实体标签人工复核尚未完成。

## 结论

采用 AliMeeting Eval、ASCEND 与 HI-MIA-CW 的最小候选组合，不再下载 MagicData-RAMC 与 MUSAN。当前下载量约 4.97 GiB，提供会议/近讲、普通话-英语 code-switch、潜在噪声/口音候选和中文混淆词负样本；与原组合相比减少约 26 GiB 下载，并避开 MagicData 的 `CC BY-NC-ND 4.0` 限制。首版公共代理集只使用 AliMeeting 与 ASCEND；`noise` 必须补测 SNR/混响，`accent` 必须人工审听，不能仅由 far-field 或香港来源自动贴标签。

这些数据作为“本项目用途相近的公共代理集”，不是本产品用户真实录音，也可能已进入候选模型训练。它只用于转换/封存管线验收、Dev/标注校准、延迟资源和公开代理证据，不与正式 Core/Reserve 合并，不能声称是未见数据或据此完成生产选型。正式选型仍需在任何模型输出可见前冻结已授权目标域 blind；缺少该语料时结论保持 `Experimental`。本轮未扫描或读取个人录音目录。

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
| Proxy Core | `R8001_M8004`、`R8003_M8001`、`R8008_M8013`、`R8009_M8019` | 13 | far-field、meeting 候选 |
| Proxy Reserve | `R8007_M8011`、`R8009_M8018`、`R8009_M8020` | 8 | far-field、meeting 候选 |

远场抽样固定使用同一阵列通道，不对 8 通道做平均混音；单流 CER 只采用无跨 speaker overlap 的完整标注区间。含 overlap 的连续会议保留为 secondary stress set，使用多说话人/时间轴指标，不混入单一文本 CER 主指标。near/far 同一会议必须绑定同一 split，并以 `content_group_id` 防止跨 split 泄漏。

### ASCEND

| Split | Rows | 实测时长 | Speakers | 用途 |
|:---|---:|---:|---:|:---|
| train | 9,869 | 31,589,400ms | 18 | Dev / Public 候选池 |
| validation | 1,130 | 3,323,603ms | 3 | Proxy Reserve code-switch / accent 候选 |
| test | 1,315 | 3,302,971ms | 2 | Proxy Core code-switch / accent 候选 |

三个 split 的 speaker 集合实测互斥。`session_id` 数字在不同 split 中重复，因此冻结 ID 必须使用 `ascend:<split>:<session_id>` 命名空间，禁止按裸整数判断 session 隔离。

### HI-MIA-CW

16,343 条 16kHz WAV，35 个 speaker，均有逐文件 transcription；内容是“Hi, Mia”中文混淆词，适合作为 non-target speech negative。实测绝大多数文件的 frame 数不能整除 16，无法在不裁剪、不补齐的前提下得到当前 manifest 所要求的整数毫秒时长，因此不进入 `public-proxy-v1-20260825`。它保留为独立的误触发/幻觉专项候选，后续应以 frame 级 negative 协议单独冻结，不进入正文本 CER 分母。

## 已冻结公共代理集

`public-proxy-v1-20260825` 已于 2026-08-25 在项目外目录确定性生成和封存。seed 固定为
`asr-public-proxy-v1-20260825`，只选择 1–20 秒的完整 utterance 或无跨 speaker overlap 的完整
AliMeeting turn；没有补音频、裁剪、跨片段拼接或 8 通道平均混音。

| Split | 样本数 | 精确时长 | 场景配额 | Speaker |
|:---|---:|---:|:---|---:|
| Proxy Core | 1,185 | 3,600,000ms | meeting 30m、code-switch 10m、clean 20m | 14 |
| Proxy Reserve | 859 | 2,700,000ms | meeting 20m、code-switch 8m、clean 17m | 11 |

Core/Reserve 的 `content_group_id` 交集为空；PCM、manifest、references 和 provenance 均位于
`~/.cache/voice-realtime/benchmarks/asr/corpora/` 下，项目仓库不保存模型、音频或逐字稿。公开代理
manifest SHA-256 分别为 Core `5fc2a7a10599140090b0e71a3dfd9b564bd55439d01fa21139dc153b1fb9e357`
和 Reserve `04385bd2dbff9b011b0c0792cad468711a2bf3ac5c7b00867628407df12c1695`；references 保持 `000`
封存权限，直到显式评分阶段才开封。

## 冻结前验收条件

- [x] 所有原始归档/固定 parquet 位于项目外目录。
- [x] 原始制品大小、SHA-256、归档安全与关键 schema 已验证。
- [x] Core/Reserve 可分配到互斥 AliMeeting session/speaker；ASCEND split speaker 互斥。
- [x] 三个公共候选来源许可均按上游 `CC BY-SA 4.0` 记录。
- [x] 采用固定 seed `asr-public-proxy-v1-20260825` 生成公共代理候选顺序。
- [x] 公共代理配额精确等于 Core 30/10/20 分钟和 Reserve 20/8/17 分钟。
- [ ] `noise` 具有实测 SNR/混响或明确环境证据，`accent` 经人工审听，`entity` 经冻结词典与人工复核。
- [x] 公共代理输入/reference 使用独立 corpus version 和目录；正式目标域 blind 将使用另一身份，指标不得合并。
- [ ] 正式 Core/Reserve reference、manifest、cluster、provenance 与 `analysis-plan.json` 在任何 blind 输出产生前同时封存。
