# Inner OS P0 评测协议

评测集只使用合成会议内容和固定测试 UUID，不提交模型输出。40 道题分为三类会议、30 道可回答的事实/草稿题和 10 道证据不足题。

两名评审者独立盲评 usefulness（1–5 分）：1 无法使用，2 大幅修改，3 部分有用，4 仅需措辞修改，5 可直接使用。分歧由第三方仲裁。报告只保留聚合指标和失败类别，不写真实原文、问题、答案、完整路径或个人信息。

指标按计划中的 evidence validity、coverage、safe insufficiency、draft usable 和 effective answer 公式计算。P0 结论只能在真实 40 问运行和盲评完成后填写；没有真实运行数据时状态必须保持 `pending`。
