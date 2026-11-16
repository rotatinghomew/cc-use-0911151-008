跨区域智慧教育应用成效与公平评估平台

scale_reviews.open 使用一张已发布评分卡及固定评分门槛建立学校扩容评议，评分不足的学校不进入待审队列。
每轮评议固定比较人口、证据指纹和总名额；decide 确认或暂缓单校申请，安全门禁仍为 hold/reject 时不能批准。
多校共享本轮名额，完成结论不可覆盖。scale_reviews.inspect 可重开数据库核对候选、名额和历史依据。

同批评分卡：run 将同一次比较中的学校决策、记录版本、证据指纹
和评分固化到 cohort_card；在同一数据库内可通过 repository.cohort_card(run_id)
重读已发布评分卡。清单重放幂等，新证据不得静默覆盖旧卡。

分阶段成效分析：staged_rollout.analyze 按真实启用时点划分启用批次，
以尚未启用的学校为对照做双重差分，核验启用前趋势与可比范围，
并说明失访与语言群体差异对结论的影响，结论为 supports/inconclusive/not_supported。
扩容批准必须引用分析证据及适用学校：scale_reviews.attach_analysis 绑定证据，
decide 校验分析结论、可比范围与学校限制来源，不再只凭总分表放行。
改期或资格更正后，attach 新分析并以 reassess 重新评估受影响的待审学校；
已批准或暂缓的结论保持当时口径。证据不足的学校在 inspect 中可见具体限制来源，不默认扩大名额。

运行验收：
python3 -m learning_evidence run --fixture fixtures/acceptance.json --database /tmp/learning_evidence.sqlite3 --output /tmp/learning_evidence-output

核验：
python3 -m learning_evidence verify --database /tmp/learning_evidence.sqlite3 --output /tmp/learning_evidence-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.programs.test_contract -v

构建检查：
python3 -m compileall -q learning_evidence tests
