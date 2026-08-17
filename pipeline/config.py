"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.33.6"
VERSION_DATE = "2026-08-17"
VERSION_NOTES = (
    "v0.33.6 fix: 处理 Codex 评审的 10 条,全部属实、全部已修。其中三条是我自己"
    "在这轮改造里新引入的缺陷,一条是把原有能力砍掉了。"
    "(1)【P1 砍错了对抗性】MAX_DEBATE_TURNS 4 → 6。轮次语义是偶数轮中书省、"
    "奇数轮门下省,而末轮门下省会强制放行 —— 4 轮展开是:提案(0) 审议(1) 修订(2) "
    "强制放行(3),也就是只有一次真实审议,而且【修订后的方案从来没被审过】。"
    "修订恰恰是最需要复核的那一版(它是按上一轮质疑改出来的)。改 6 轮后有 3 次"
    "真实审议。配套:末轮不再凭空合成 approved,而是照常调门下省、拿到质疑后"
    "再强制放行并把未解决的质疑写进 overall_assessment(多花一次 deepseek-v4-flash,"
    "换回'交付方案一定被对抗性看过至少一遍')。"
    "(2)【P1 评分口径串代】vibe_loop 撞轮次上限退出时,上一步刚做完重写 —— "
    "prompt_matrix 是新正文,_vibe_cell_reviews 却还是改写前的判词,评分器拿旧的"
    "multiplier_gate 配新的 demo,实验里会把改进读成退步、退步读成改进。这正是 "
    "P1 建评分体系要避免的失真,不修等于评分器自己制造噪声。处理是【作废】不是"
    "补评:删掉后对应维度返回 None(=没测),不进高分篇计数,coverage 会掉下来,"
    "UI 已有'覆盖不满→数字不可比'提示。"
    "(3)【P1 读错字段】叙事导演重建限流按 severity 排序,但契约里是 priority —— "
    "全部条目落到同一兜底档,排序变 no-op,cap 成了'按模型返回顺序砍',后面的 "
    "high 会被前面的 medium 挤掉。两个名字都认,priority 优先。"
    "(4)【P1 假装重建过】限流只裁了局部列表,review 里的 cells_to_revise 没动,"
    "而终审的 narrative_director_summary.cells_rebuilt 正是从它推的 —— 被推迟的 "
    "cell 以'已重建'身份呈给终审,而 chancellery.md 明写'cells_rebuilt 非空 → "
    "优先抽查它们的 demo',于是终审去抽查一个根本没重建的 cell,那道检查废掉。"
    "同步裁剪 review。"
    "(5)【P1 fail-open】消费者模拟按 weak 过滤时,把任何非空 _vibe_cell_reviews "
    "都当成覆盖完整。critic 响应截断只返回一个 pass 的 cell 时,_weak_ids 为空 → "
    "整个矩阵被跳过、还对外宣称'没有 weak 档'。改为对全量 cell_id 校验覆盖度,"
    "不全则退回全量复判。"
    "(6)【P1 伪精确误伤事实】单位表原收 天/周/年/次/遍,但这些有大量合法产品事实"
    "用法(质保 1.5 年 / 临床随访 2.5 年 / 每天 1.5 次),而规则自己的理由写的就是"
    "'产品规格属事实层照常精确',自相矛盾。硬档收窄到只有秒/分钟/小时(那几个带"
    "小数几乎只可能是编的行为量),其余降 SOFT 交给 critic 看上下文。"
    "(7)【P2 我自己犯了三层摘要漏斗】v0.33.5 给吏部加的 origin_path / "
    "pet_phrases / number_memory 三个字段,全库搜只出现在 personnel.md 和版本注记里"
    "—— works.md 重建 persona_library 用的是一份固定字段表、不含这三项,"
    "cell_planner 和 builder 也不引用。字段死在下一层,等于吏部白写。讽刺的是"
    "'三层摘要漏斗'正是本轮最初审查里我自己点名的问题。现已打通四处:works.md "
    "字段表 + JSON 示例、cell_planner 精炼原则、builder 必嵌模块(含三条使用纪律)。"
    "(8)【P2 指纹告警没人看】跨篇指纹跑在终审之后、影响不了 verdict,而 pages/3 "
    "和 pages/4 都不读 _prose_gate —— fingerprint_hits > 0 时产出照样出货,告警只"
    "存在于服务端日志。这正是本轮反复强调的'无声截断会被读成这些问题不存在',"
    "不能自己再犯。已加流水线详情页面板:两个指标 + 逐条列出重合的模板/插入词/"
    "开头结尾指纹 + 逐格子明细。"
    "(9)【P2 seed 身份错位】采样失败后 samples 被压缩,enumerate 会给成功样本安上"
    "错的 seed(seed 1 失败、21 成功时记成 seed 1),而 per-seed 诊断正是用来判断 "
    "seed 轮转有没有生效的。改存 (seed, text) 对。"
    "(10)【P2 欠采样当健康数据】只回来 1 篇时 measure_diversity 报首句去重率 "
    "100%(一篇当然不重复),矩阵和 UI 当成功收下 —— 而这最容易发生在限流/部分故障"
    "时,于是 baseline 被污染成'越不稳定看起来越好'。少于 3 篇标 status=partial,"
    "不计入聚合。"
)
_VERSION_NOTES_V0335 = (
    "v0.33.5 feature: 机审闸 prose_gate + 人味层四处提示词修正。"
    "(1) 新增 pipeline/prose_gate.py:机械可判的文字指纹检测收归一处。定位是"
    "【机械下沉、判断上留】—— 去 AI 味规则此前以禁令清单散在礼部产出和工部装配"
    "的 prompt 里,代价是信号稀释(注意力从'写'转到'查')、防御性写作(一边写一边"
    "躲清单,稿子会紧)、以及让 LLM 查禁词(用判断力干正则的活,烧 token 还带方差)。"
    "(2) 新增能力:翻案句家族(不是X而是Y/看似实则/与其说不如说)、商业黑话"
    "(赋能/闭环/底层逻辑)、伪精确行为量(1.7秒式);跨篇指纹(共用插入词/共用修辞"
    "模板/开头结尾指纹/高频四字串)。跨篇这层是**新能力不是重复建设**:已有的"
    "跨 cell 查重抓【词面重合】(换汤不换药),这里抓【结构重合】(换药不换汤)——"
    "每篇都插一句'说真的'、每篇结尾同一句式,词面相似度可以很低但读者一眼流水线。"
    "(3) ⚠️【三档分级是必需的,不是可选项】。小红书有大量自己的方言 ——"
    "「家人们」「谁懂」「闭眼入」「救了我的」,而 foundation.md 明确把它们列为"
    "**平台暗号词、真实感信号**。这些词单篇出现是人味,十篇都有才是流水线。"
    "放进单篇硬禁 = 机审闸和 foundation.md 打架,而 foundation.md 是对的。"
    "所以它们只进 BATCH 档按跨 cell 占比判(插入词 ≥60%、修辞模板 ≥40%)。"
    "同一个词在不同层里是不同性质的东西 —— 移植任何外部规则表时最容易搞错这点。"
    "(4) 因此【自建规则表而非移植现成脚本】。外部中文 AI 腔脚本校准场景是知乎"
    "长文/公众号,直接移植至少三处误伤:冒号全禁(小红书正文『成分:』『价格:』"
    "是常规写法、标题『XX:一定要看』是原生句式)、家人们/姐妹们、谁懂/闭眼入。"
    "另两处口径放宽:伪精确单位表只收时间和次数不收克/毫升/%(那些是产品规格、"
    "属事实层);蓝词走白名单(必须原样重复,轮换同义词直接损伤搜索权重,"
    "出现在每篇里是正确行为)。"
    "(5) 接入位置在 vibe_critic **之后**做合并而非之前做过滤。有一种说法是"
    "'机审前置能让 critic 和二审调用量下降',这是错的 —— 两者都是批处理"
    "(整批一次调用),过滤 cell 只降输入 token 不降调用次数,而且拦下的 cell 走"
    "rewriter + 复扫可能反而增加轮次。放 critic 之后是纯加法:借它已产出的 "
    "failed 列表顺路把机械命中的 cell 送进 rewriter,零额外调用。真正的省在"
    "下一轮 —— round 2+ 复扫免费,且能抓到 rewriter『把 A 指纹改成 B 指纹』这种"
    "机扫过了读者没过的劣化。"
    "(6) quality_metrics.check_redlines 改为 prose_gate.scan_text 的薄封装,"
    "不再自带词表 —— 两边各带一份必然漂移,而漂移的表现最难查:分数还在涨,"
    "闸门已经按新规矩走了。副作用是红线层跟着变强(新增上述三个家族),这是想要的。"
    "(7) 提示词四处修正:①vibe_rewriter/structural_rewriter 加【修复哲学】段 ——"
    "禁止用另一种漂亮句式替换(把'不是X而是Y'改成'与其说X不如说Y'只是 A 指纹换"
    "B 指纹)、禁止用固定语调词替换逻辑连词('所以'→'你看''说白了'是用一套语调"
    "模板换掉一套逻辑模板)。这堵的是管线里最隐蔽的劣化路径。②吏部 schema 增"
    "origin_path/pet_phrases/number_memory —— 活人感最高杠杆是【知识的来路】,"
    "原 schema 有 posting_motivation(为什么发帖)但缺'怎么接触到产品',人设只是"
    "标签卡;三条纪律:来路每篇只露一两项(凑齐会变成新模板)、口头禅是人设自己的"
    "(全批共用的插入词单篇是人味十篇是指纹)、数字要有来路。③礼部 format_specs "
    "去掉 opening_patterns/closing_patterns —— 【礼部在批量生产批量指纹】:这两个"
    "字段进 shared_skeleton 被所有 cell 共用,十个格子从同一份模板挑开头,而且这种"
    "重合词面相似度低、跨 cell 查重抓不到。改成输出【纪律】而非【模板】,开头多样性"
    "交给 opening_angles 的 15 条编号角度。anti_ai_checklist 改预算制(≤5 条、"
    "每条带原因)——只列禁什么模型会绕过字面继续踩同类错。④刑部加数据背书红线"
    "(无出处的'有数据显示/世卫组织建议'一律禁,软植入无法向读者标注虚构,编出来"
    "的数只剩造假一种身份)。⑤终审加删末段测试(删掉最后一段更有力就判结尾冗余,"
    "AI 在结尾多绕一圈是最稳定的习性之一,而且单句级检查抓不到)。"
)
_VERSION_NOTES_V0334 = (
    "v0.33.4 feature: 跨批次多样性 —— 交付物侧的补偿机制。"
    "(1) 病灶:交付的 system_prompt 有 {{seed}} 做批内差异化,但【没有任何跨批次"
    "机制】。运营不是跑一次就完,是每天跑、连着跑几十批 —— 跑到第 10 批必然撞车,"
    "而且撞了没人看得出来。"
    "(2) 修法是【给现有的开头切入池扩容并编号】,不是新加一套机制。5 池里的 "
    "opening_angle 原来只要求'至少 5 个',5 种粒度的组合跑 5-10 批就用光;拆到 "
    "15 种(C01-C15)能撑 20-30 批。这是纯粒度问题。不新加机制是因为 "
    "works_builder.md 的字符预算刚在 v0.33.1 收紧过(13 个必嵌模块 ≈2400 字 / "
    "上限 3000),硬塞一套 500 字的新机制会把刚修好的预算再撑爆 —— 那就是一边修"
    "一边破。扩容只多约 100 字符,预算表同步更新到 ≈2680 并补了'压不下时按什么"
    "顺序牺牲'的指引。opening_angle 是五池里【唯一】要求满 15 条的,其余四池仍是"
    "'至少 5 个':它们管批次内差异化,只有开头角度还要管跨批次。"
    "(3) 编号形式是为了让回避能落地 —— 运营粘贴『上批已用:C03 C07 C11』比粘贴"
    "一堆角度描述可操作得多。两条条件规则内置进交付的 prompt,用户不填就正常跑:"
    "used_angles(跨批次回避)和 account_profile(多账号从根部分岔,避免'5 个账号"
    "发出来像一个人写的')。批量输出格式也要求每篇标出本篇的 C 编号 —— 不标就"
    "没法回避。"
    "(4) 校验:_validate_prompt_cell 数 system_prompt 里的 C01-C15 编号,少于 10 条"
    "报【软告警】(不触发重试,遵循本函数其余检查的尺度)。加这道是因为不加的话"
    "'15 条编号'就又变成一个没人验证的承诺 —— 正是本轮改造批评过的'存在 ≠ 有效'。"
    "只数编号不查内容:C01-C15 在正常中文里几乎不会误命中(实测『维生素C』/"
    "『C 罩杯』/『C16』/『C99』全不命中),是个高精度信号。"
    "(5) 诚实边界:产出中心新增一段交底,明说这是【补偿不是根治】。大模型是无状态"
    "的,不知道昨天用同一条 prompt 生成过什么;真正的跨批次去重要工作台做三件事"
    "(已生产资产库 / 自动注入避重池 / 账号自动分档),没做之前跑到第 20-30 批仍会"
    "开始撞车,届时该推的是工作台改造而不是继续改 prompt。不写清楚的话,运营跑到"
    "第 10 批发现重复会以为是 prompt 写坏了。"
)
_VERSION_NOTES_V0333 = (
    "v0.33.3 feature: 批量采样验收 —— 补上'用 1 篇 demo 验收一个批量生成用的"
    "prompt'这个根本缺口。"
    "(1) 病灶:交付物是给批量生成用的 system_prompt(要产出 N≥10 篇),但流水线"
    "里 11 道质量闸(红蓝/画像×2/结构审/网感 critic/二审/消费者模拟/终审)看的"
    "**全都是每个 cell 唯一那篇 demo_output**。后果有三:①决定'第 7 篇会不会和"
    "第 3 篇一个味'的 5 池 + 人设轮换机制从来没被验证过 —— 唯一的检查是"
    "kimi_structure_reviewer 数一数池子的文字在不在 prompt 里,【存在 ≠ 有效】;"
    "②n=1 测的是 demo 的质量不是 prompt 的分布 —— 那篇 demo 被红蓝精修 + 网感"
    "重写 + 结构补漏打磨过最多 3 轮,反映的是'这 8 道工序能把一篇修到多好';"
    "③优化方向是反的 —— 目标是上限(100 篇里 5 篇爆款 > 100 篇都 85 分),而"
    "最贵的两个环节干的恰恰是把唯一样本往均值推,n=1 下无法区分'抹掉了尾部烂篇'"
    "和'抹掉了头部爆款'。"
    "(2) 新增 pipeline/batch_sampler.py:拿最终 system_prompt 真跑 N 篇"
    "(默认 5),**只变 {{seed}}**,topic 固定、persona 留空。只变 seed 是因为"
    "那正是 works_builder.md 批量规则承诺的差异化开关(『相邻两篇 seed 间隔 "
    "≥20』)—— 用它自己承诺的口径去测它;persona 留空是有意的,手动指定人设"
    "等于替 prompt 做了它本该做的事,那条轮换规则就测不出来了。"
    "(3) 三个指标:样本红线通过率(追 100%)、首句去重率、两两相似度"
    "(字符 trigram Jaccard)。后两个取【各 cell 的最差值】不取均值 —— 一个格子"
    "严重趋同就是一个格子的批量废掉了,被别的格子平均掉就看不见。相似度这一项"
    "是必需的:实测『每篇开头都不同但正文只换了几个词』的样本,首句去重率 100%、"
    "相似度均值 0.45,光看去重率会放过它。"
    "(4) 【观测,不拦】(mode=observe_only):不阻塞出货、不触发重写、不影响 "
    "verdict。因为仓库现在还没有历史分布,没有基线就设阈值等于凭猜调参 —— "
    "那正是这轮改造要治的病。先攒几条 run 的真实分布,再决定阈值和要不要升级"
    "成闸门。"
    "(5) 成本:走辅助层(kimi_client)不占 MAX_TOKENS_PER_RUN,system_prompt "
    "命中 prompt cache(K2.6 省 83%),每次只出一篇。12 格子 × 5 篇 ≈ 60 次 ≈ "
    "$0.15-0.3/run,通过 accumulate_auxiliary_cost 单独上报 —— 悄悄花钱比"
    "花钱更糟。采样量超上限时【显式报告】未采样的 cell,不静默截断。"
    "(6) kimi_client 新增 call_kimi_text(纯文本,不做 JSON 解析)—— 采样要的是"
    "模型按 system_prompt 写出来的一篇内容,那本来就不是 JSON,套 call_kimi_json"
    "会在 _extract_json 退化成 _parse_error。两者共用 _call_kimi_raw。"
    "采样用 asyncio.to_thread 跑,这是辅助层唯一一处这么写的:同步 client 在"
    "事件循环里直接调会把 60 次采样串行化(几分钟),量级不同写法就得不同。"
)
_VERSION_NOTES_V0332 = (
    "v0.33.2 perf: 按 token 而不是按调用次数重排成本,砍掉四处高消耗低收益。"
    "⚠️ 方法论前提:先按【调用次数】排过一版成本序,结论是'红蓝精炼跑全量最贵'。"
    "按 token 重算之后那个结论是错的 —— 红蓝的提示词只有 1848 字符、输出是个"
    "attacks 列表、而且红队找不到问题就直接跳过蓝队(已有优化),token 上很便宜。"
    "真正的消耗集中在:工部构建、策略辩论的 k3 输出、终审的 k3 输入。所以本版"
    "【不动红蓝】,改打这四处:"
    "(1) 终审输入 allowlist 瘦身 —— 单项收益最大。chancellery_final 跑 kimi-k3"
    "($3/$15,单价最高),拿到的是【整个 final_system】,而 chancellery.md 全文"
    "只引用 prompt_matrix 和单独注入的 narrative_director_summary。demo 正文被"
    "送了三遍(prompt_matrix.demo_output / demo_outputs[].output_content /"
    "_red_blue_stats.details[].refined_demo_output),加上 _persona_reactions、"
    "_structure_review、_consumer_simulation、vibe_critic_result 这些提示词一个"
    "字没提的诊断包。12 格子实测 104K 字符 → 54K,砍 49%。这是 v0.30.5 修的那批"
    "dead drop 的镜像:那次是'注入了但提示词不知道读'(漏信号),这次是'注入了而"
    "提示词根本不需要读'(烧钱),同一个根因 —— 没人管 final_system 上该有什么。"
    "用 allowlist 不用 blocklist:后者挡不住'以后又有人往上挂新字段'的复发路径,"
    "而那正是问题来源;丢掉的 key 打日志让 drift 可见。详见 architecture.md 第 6 节。"
    "(2) MAX_DEBATE_TURNS 8 → 4。中书省每轮重吐完整大 plan(5-7 directions +"
    "matrix_skeleton)跑 k3 的 $15/1M 输出档,正是它把 MAX_TOKENS_STRATEGY 逼到"
    "48000 的。砍的理由不是省钱,是【对抗性本来就是跛的】:config 自己在"
    "THINKING_STAGES 承认门下省跑 deepseek-v4-flash、端点不认 thinking 参数、"
    "所以实际没有 extended thinking。'k3 深推理'对'廉价档浅判断',第 3、4 个"
    "来回的边际收益很低而成本线性翻倍。门下省仍可提前 approve,4 是上限非固定开销。"
    "(3) 消费者模拟只跑 critic 判 interest_align=weak 的 cell。它调的【就是同一个"
    "persona_simulator】,而同批 demo 已被画像 agent 判过两遍(主+alt),这是第三遍;"
    "且它自称'interest_align 的第二层校验',可 vibe_critic 的四乘数硬门槛已经逐 cell"
    "判过 interest_align —— pass 的复判多半同答案,fail 的早进了 rewriter/告警。"
    "只有 weak 那档(critic 自己都拿不准的)值得花第二票。数据取 P1 建的"
    "_vibe_cell_reviews,零额外成本;拿不到 review 时退回全量(critic 挂了时恰恰"
    "最需要这层校验)。结果里写 _scope 标明复判范围,避免 UI 上'全部通过'被读成"
    "'整个矩阵都过了'。"
    "(4) 画像弱 cell 判据:任一 backend 否决 → 两个 backend 都否决。改的是【成本"
    "不对称】不是准确率:并集规则下多一个 backend 主要提高误报率,而误报代价极不"
    "对称 —— 弱 cell → strategic_warnings → 触发 strategic_escalation → 回中书省"
    "(k3)改方向 → 再跑一整轮 vibe_loop。一次几分钱的 DeepSeek 调用能触发全流水线"
    "最贵的重入。交集要求跨厂家一致才算弱,这才是当初做双 backend 的本意。只有一个"
    "backend 跑通时自动退化成它自己,不会因为少一票就永远判不出弱 cell。"
    "(5) 叙事导演重建上限 3。诊断便宜(2101 字符提示词、只看 demo 前 500 字),贵的"
    "全在重建 —— 每个 flagged cell 重跑一次 works_builder(token 消耗第一的 stage),"
    "诊断出 8 个就等于把最贵的 stage 又跑 8 次。它管的跨 cell 撞车另有两处覆盖"
    "(_find_cross_cell_duplicates 确定性零成本跑两遍 + vibe_critic 第 3 步),"
    "三重覆盖里只有这一路触发最贵的重建。超出上限的按 severity 排序后转"
    "strategic_warnings 交人工,不静默丢弃 —— 无声截断会被读成'这些问题不存在'。"
    "(3)(4)(5) 全部走 config flag,默认新行为,改一行即可回退。"
)
_VERSION_NOTES_V0331 = (
    "v0.33.1 fix: 拆三颗互相独立的雷 —— 约束打架 / 结构审重复 / 路径撞车。"
    "(A) works_builder 的输出预算是【自相矛盾】的,这是 v0.32.3、v0.32.4 反复"
    "出现空烧和截断的真根。真因不是'模块多字数少',是 demo 正文在输出里写了"
    "两遍:prompt_cells[i].demo_output 一遍,demo_outputs[].output_content "
    "又一遍。而 orchestrator 第 6.95 步会从(已精炼的) prompt_matrix 整个重建 "
    "demo_outputs,只留 persona_used —— 那份副本 100% 被丢弃。小红书 demo "
    "300-800 字写两遍,直接把'单 cell ≤4500 字符'撑爆(2500+800+800+400 = "
    "4500 起步),超了就截断→批次重试→单 cell 重试的三倍空烧。修法:"
    "demo_outputs 只保留 cell_id + persona_used;顺带把 system_prompt 上限"
    "从 1500-2500 调到 2000-3000 并给出逐模块的字符预算表(按 13 个必嵌模块"
    "实测最低约 2400,原来的 2500 上限等于零余量、1500 下限根本不可能达成);"
    "另修正规则编号重复(有两个 4. 和两个 5.)——在一个要求模型'回头数一遍'的"
    "提示词里编号乱掉不是洁癖问题。"
    "(B) 结构审 6 项里有 5 项 _validate_prompt_cell 早就用别名表确定性查过了"
    "(五池/人设/合规存在性/关键词存在性/禁用清单存在性),花一次 LLM 调用重查"
    "是纯重复。第 6 项『平台调性张冠李戴』其实也是确定性的(平台调性词表固定),"
    "一并下沉到 quality_metrics.check_platform_voice。结构审提示词收敛到它"
    "真正独有的职责:判断合规/关键词/禁用清单写的是【可执行的具体规则】还是"
    "【应付差事的空话】——『注意合规即可』和『不得声称根治』字符串匹配都能查到"
    "'合规'二字,只有模型分得出前者等于没写。新增 evidence 必填(原文摘抄),"
    "堵住凭印象打分。确定性结果走已有的 _structure_hint 通道交给重写器,"
    "不触发 builder 重试;模型挂掉时确定性那半边照常工作。"
    "平台调性判定有意压低召回换精度:只有『自己平台调性词一个没命中 + 某个别家"
    "平台命中 ≥2 个』才报 —— 单个外来词很可能是『不要写成抖音那种前3秒』这类"
    "反例引用,按命中即报会误杀。"
    "(C) 8 条突破路径的跨批次撞车(v0.30.6 记的 M2 遗留项)。cell_planner 分批"
    "并行(BATCH_SIZE=5/CONCURRENCY=5),批次之间互不知道对方选了什么路径,"
    "12 格子分 3 批同时跑撞车既必然又不可见 —— 而路径组合正是'几个方向打法不"
    "重样'的核心机制。修法:orchestrator 在分批【之前】按 direction_id 确定性"
    "预分配(PATH_LIBRARY + 互质偏移轮转 0/3/5,方向数 ≤8 时保证两两不同),"
    "全量表塞进每个批次和单 cell 重试的输入。按 direction 而非按 cell 分配:"
    "矩阵是 方向 × 平台,要不重样的是方向之间,同方向跨平台本就该共用一组。"
    "方向编号按数字后缀自然排序,让分配不依赖 active_cells 到达顺序,"
    "避免 resume 重跑时路径漂移。零 LLM 成本。"
)
_VERSION_NOTES_V0330 = (
    "v0.33.0 feature: 双层评分体系 —— 本仓库第一个【输出侧】质量度量。"
    "(1) 病灶:24 个 stage、11 道质量闸,但没有任何地方回答得了'这一版产出"
    "比上一版好吗'。R-022 飞轮 audit 追的是'数据库样本有没有被用上'"
    "(输入侧遥测);config.py 几百行版本注记全是崩溃/截断修复,没有一条是"
    "质量增量。后果是改提示词全凭手感:改完 works_builder.md 跑一条 run,"
    "觉得 demo 看着顺眼就当改对了 —— 样本量 1,还是被红蓝 + 网感重写打磨过"
    "3 轮的那 1 篇。"
    "(2) 新增 pipeline/quality_metrics.py,在 save_output 之前给最终 "
    "prompt_matrix 打分并落 stage_logs(stage_name='quality_score')。"
    "【零 LLM 成本】:红线层是纯 Python 确定性判定;高分层从 vibe_critic "
    "已产出的 cell_reviews 里提取(multiplier_gate 四项 + template_test),"
    "不重复判。"
    "(3) 两层目标不同,不能混:红线层追 100% 通过率(hill-climbing);"
    "高分层追【高分篇绝对数】翻倍,有意不提供 rate 字段 —— 把它当 pass "
    "rate 优化会让改动倾向消除尾部低分篇,分布向均值收窄,把 95 分压到 "
    "85 分换 60 分升到 75 分,方差消失=爆款消失。"
    "(4) 三条设计约束(改动时不要破坏,详见 docs/architecture.md 第 5 节):"
    "① 红线是准入高分是排名 —— 有红线违规的 cell 不进高分篇计数,单独标 "
    "high_score_blocked_by_redline(这类修复性价比最高);② 红线层只收"
    "'命中即死'型黑名单判定,'应存在'型检查一律放高分层 —— v0.32.3/"
    "v0.32.4 两次三轮空烧事故都是'应存在'型误判造成的,黑名单假阳性率"
    "天然低而'应存在'假阴性率天然高;③ 工艺四要素按 paradigm 分流 ——"
    "「具体性四要素」是 works_builder.md 写在【范式 A 专用】段下的,范式 B"
    "靠术语锚和结构建立信任,拿 A 的尺子量 B 会系统性低估所有 B 格子"
    "(实测 foundation.md 里那条当范例的真实洗洁精爆款按 A 判缺 3 项)。"
    "(5) vibe_loop 跨轮次按 cell_id 累积 cell_reviews —— round 2+ 只复检"
    "被改写过的 cell,单看最后一轮会漏掉 round 1 就通过的格子。累积结果存"
    "在 orchestrator 实例上【不是 final_system 上】:后者会被整体透传给 "
    "chancellery_final(kimi-k3 $3/$15),挂上去等于给最贵的 stage 白加"
    "十几 KB 输入换零收益。"
    "(6) 流水线详情页新增质量评分面板:红线通过 / 高分篇 / 评分覆盖三个"
    "指标分开显示 + 违规分布 + 逐格子明细。覆盖度掉下来时显式提示'高分层"
    "数字不可比',避免把 critic 故障误读成质量下降。"
)
_VERSION_NOTES_V0325 = (
    "v0.32.5 fix: 账户欠费被停(429 suspended / 402 Insufficient Balance)"
    "不再当普通限流重试。现场(2026-08-04 08:10):run 已跑到终审,Moonshot "
    "余额烧干,终审对着 suspended 账户重试 3 轮 × 3 个阶段后 run 以一堆无关"
    "堆栈挂掉。现在:主链路和辅助层都识别欠费指纹,立即停止重试,错误信息"
    "直接写'去充值,回详情页点继续执行(已完成阶段不重跑)'。"
)
_VERSION_NOTES_V0324 = (
    "v0.32.4 hardening: 卡死/空烧排查的第二轮,拆两颗同类雷。"
    "(1) _validate_prompt_cell 的 essential_keywords(合规/关键词/反 AI 腔"
    "禁用清单)是【硬失败】却单字面量匹配,是全函数唯一没有别名表的检查 ——"
    "紧邻的 pool_aliases / batch_rule_aliases 都有,后者注释原话就是'别让 "
    "builder 因为措辞差异陷入三轮重试地狱'。现场日志里它已在放炮"
    "('找不到 禁止',D2/D6/D7/D8);模型措辞习惯稳定的话三轮都过不了,"
    "整条 run 直接报'三轮尝试后仍缺失'挂掉。补齐别名表。反 AI 腔那组特意"
    "只收明确是禁用清单的写法,不收'不得'/'避免'这类裸词 —— 合规段里就有"
    "'不得宣称疗效',裸词会让这条检查变成永远为真的摆设(实测踩过)。"
    "(2) MAX_TOKENS_STRATEGY 32000 → 48000。v0.30.12 踩过 thinking 挤占"
    "max_tokens 导致 secretariat 大 plan 被截断、cell 重建得 0 个、"
    "'工部·格子规划产出为空'崩溃的坑,v0.30.13 靠关掉辩论期 thinking 绕开。"
    "换厂后这颗雷重新装上:四个策略阶段都在 THINKING_STAGES 且跑 kimi-k3 "
    "走 adaptive,Moonshot 是否把 thinking 计入 max_tokens 无明确文档。"
    "max_tokens 是上限不是预付费(K2.6 标称可到 262144),抬天花板几乎不"
    "花钱,而截断的代价是三轮重试甚至整条 run 崩。"
    "(3) 补 `from typing import Any`。orchestrator 有一处局部变量注解用了"
    "未导入的 Any —— PEP 526 下局部注解不求值所以从没崩过,但那行一旦被挪到"
    "模块/类作用域就会 NameError。基线(fba746e)就存在,顺手拆掉。"
    "另:本轮复核了所有循环边界(辩论 8 轮 / 终审 3 轮 / 网感 3 轮 / 策略升级 "
    "1 轮 / 澄清 2 次且有 1 小时超时 / 限流器窗口必然老化 / 心跳 10s vs "
    "reaper 75s),均有界,未发现真正的死循环。"
)
_VERSION_NOTES_V0323 = (
    "v0.32.3 fix: 工部构建 3 倍空烧的两个真因(这才是撞 token 上限的根)。"
    "(A) _validate_prompt_cell 的结尾完整性检查在小红书上 100% 误判。"
    "小红书笔记本来就以话题标签收尾('...下月再汇报。 #按摩椅 #中秋送礼'),"
    "但检查只认句末标点,于是每个格子都被判'demo_output 结尾不完整'→ hard "
    "fail → 批次重试 → 单 cell 重试,三轮后拿到同样的合法内容,只好 "
    "'accepting anyway (best effort)' 收下。等于每格固定烧 3 倍 token 换回"
    "同一个结果。修法:判定前先剥掉尾部话题标签(半角#和全角＃都认),剥完"
    "按正文结尾判 —— 真正停在半句话上的截断仍然抓得到。resume 时那句"
    "'rejected 45 cells ... will be rebuilt' 也是同一个误判,一并消失。"
    "(B) _reconstruct_active_cells 会把同一个格子 splice 两遍。中书省把 "
    "platform 写成'小红书'而 brief 的 target_platforms 是'xiaohongshu'时,"
    "两边 _platform_key 不相等 → 所有 pair 判为缺失 → 原样再补一遍,"
    "active_cells 里出现完全重复的 cell_id(D1_xiaohongshu 两次),工部把每个"
    "格子建两遍。日志指纹:missing dirs 是空的却说 missing N pairs。修法:"
    "splice 时同时按 cell_id 去重 + 末尾统一去重一次 + 命名不一致时"
    "显式 warning 指出根因。"
    "两者叠加:9 个格子 → 18 次(重复) → 最多 54 次(三轮) 调用。"
)
_VERSION_NOTES_V0322 = (
    "v0.32.2 fix: 流水线详情页的『构建批次 N』一直在骗人。"
    "(1) 那个 N 是【第 N 次调用】的流水号,不是格子数 —— 每个格子至少 1 次"
    "(Round 1),失败还有批次重试 + 单 cell 重试,最多 3 次,所以看上去像"
    "有 66 个格子实际可能只有 22 个。pages/3 的 _batch_label() 本来设计成"
    "显示 '批次 15 · initial [D6_xiaohongshu]',数据源是 input_data 里的"
    "_batch_info;但 get_stage_logs 为了控 payload 把整个 input_data 从"
    "select 里去掉了,于是这条分支从那次优化落地起就是死代码,所有行永远"
    "退回流水号。"
    "(2) 修法:不捞整个 input_data(每条几十 KB),只用 PostgREST 的 JSON"
    "路径投影把 _batch_info 单独取出来(别名 batch_info)。三级降级:带投影"
    "→ 去投影 → 去 output_data;第一级对任何异常都静默降级,PostgREST 语法"
    "万一不认也只是标签退回流水号,不会把详情页打挂。"
    "(3) 标题行现在直接报真实格子数:『工部·构建（22 个格子 · 共 66 次调用,"
    "含 batch/cell 重试）』—— cell_ids 去重得来。拿不到 batch_info 时只报"
    "调用数,不瞎猜格子数。"
)
_VERSION_NOTES_V0321 = (
    "v0.32.1 fix: 预算熔断误杀 + 熔断错误被重试逻辑吞掉。"
    "(1) MAX_TOKENS_PER_RUN 2M → 8M。v0.32.0 换厂时把这个值留着没动,"
    "判断错了 —— 它一直是【成本容忍度】旋钮(v0.31 原注释 'Tune to your "
    "cost tolerance',2M 在 Opus 单价下 ≈ $74/run)。换厂后单价掉一个数量级,"
    "同一个 2M 只剩 ≈ $5,等于悄悄把容忍度砍到 1/14。实测后果:8 方向 × "
    "2 平台 = 16 格子的正常 run,工部构建跑到第 14 个格子被强杀。"
    "(2) RunBudgetExceededError 不再被 cell_planner / works_builder 的"
    "重试逻辑吞掉。这两处的 Round 2(批次重试)和单 cell 重试都是裸 "
    "`except Exception`,预算熔断一旦发生就被降级成'这个 cell 没返回',"
    "于是每个剩余批次再白跑三轮,最后报一句误导的'works_builder 三轮尝试"
    "后仍缺失'——真实死因(token 爆了)只出现在末尾一条 batch error 里。"
    "改成和 red_blue / persona_simulator 一致:熔断先 re-raise 冒泡硬停。"
)
_VERSION_NOTES_V0320 = (
    "v0.32.0 breaking: 全链路换厂 —— Claude / GPT / Gemini 三家全部退场,"
    "改用 Kimi (Moonshot) + DeepSeek 两家。"
    "(1) 主链路 24 个 stage:策略核心 4 个 (太子/中书省/终审/工部·架构) 走 "
    "kimi-k3 (2.8T, 1M ctx);异厂家对抗的门下省/兵部走 deepseek-v4-flash;"
    "其余全部 kimi-k2.6。persona_simulator_alt 从 deepseek-v4-pro 改 "
    "deepseek-v4-flash 与主链路对齐。"
    "(2) 新增 Moonshot anthropic-compat backend (api.moonshot.cn/anthropic),"
    "按 `kimi-*` 前缀路由,复用 DeepSeek 那条 per-model 路由器;"
    "secrets.toml 新增 MOONSHOT_API_KEY / MOONSHOT_BASE_URL。"
    "(3) Gemini 辅助层整体下线:新增 pipeline/agents/kimi_client.py 接管"
    "网感二审 / 结构审 / 识图 / 截图分析四个岗位 (K2.6 原生多模态);"
    "参考帖抓取改走 SocialDataX detail (Gemini url_context 无对应物);优先 "
    "by-URL 变体,失败退 by-ID。整条链去掉 LLM —— SocialDataX 返回的本来就是"
    "第一方结构化数据,再让模型转述一遍只是徒增幻觉。google-genai 依赖移除。"
    "(4) thinking 按厂家分流:kimi-* 走 {\"type\":\"adaptive\"} (Moonshot "
    "anthropic 端点支持),deepseek-* 不发 thinking 字段 (端点静默忽略,"
    "发了只是浪费);Claude 的 adaptive 判定保留供历史 preset 用。"
    "(5) 无 Claude 中转也能启动:init_api_config 不再强制要求 "
    "[claude_relay_presets.*],只要 Moonshot / DeepSeek 任一可用即可;"
    "MODEL_FALLBACK_CHAIN 取代 CLAUDE_FALLBACK_CHAIN,且会跳过未配置 "
    "backend 的候选,不再把 'key 没配' 误当成 'no channel' 一路降级。"
    "(6) 成本表按官网实价更新 (kimi-k3 $3/$15, kimi-k2.6 $0.95/$4, "
    "deepseek-v4-flash $0.14/$0.28);Claude/GPT/Gemini 旧条目保留,"
    "仅供历史 run 的成本回显,不再有任何 stage 指向它们。"
    "⚠️ 迁移须知见 README「换厂迁移」段:secrets.toml 要补 "
    "MOONSHOT_API_KEY,VERTEX_EXPRESS_API_KEY / VECTORENGINE_API_KEY 可停用。"
)
_VERSION_NOTES_V031 = (
    "v0.31.0 feature+reliability: SocialDataX 直连 + 全链路卡死/低质量硬化。"
    "(1) #33 趋势取样从 Gemini 网搜切到 SocialDataX MCP 直连小红书(REQUIRED/"
    "fail-fast:取不到样提前终止,避免少关键素材导致产出差)。"
    "(2) #34 可靠性大修:心跳+reaper 收割僵尸 run、自适应 RPM 安全阀(60 起跑撞 "
    "429 回落 15)、GPT→Claude 跨厂商兜底、gather return_exceptions+聚合、原子 CAS "
    "启动守卫、st.fragment 非阻塞刷新、DB 写重试。"
    "(3) #35 前端重设计(去 emoji + 统一品牌 favicon)。"
    "(4) #36 三轮 8 维审计 + 对抗验证共 29 处修复(含 2 P0):终审驳回后卡死"
    "(修订态 _revision_context 跨 run 泄漏 / brief 快照覆写抹掉 round 计数)、"
    "save_output 复用 run_id 致修订产出被旧版盖、网感闸门 fail-open→fail-closed、"
    "resume 复活未批准策略方案、蓝队失败当'全过'出货、review_dimensions.score "
    "畸形致零产出、reaper 扩到 paused 僵尸、resume/revise 刷心跳防误杀、五部截断"
    "可见化、demo_outputs 与精炼后 prompt_matrix 同步 等。#9 限流器 reconfigure "
    "按决定不改。⚠️ gpt-5.5/deepseek-v4-pro 成本单价为估算值,待按账单校准。"
)
_VERSION_NOTES_LEGACY = (
    "v0.30.13 fix: 回退 strategy-debate thinking(修 v0.30.12 引入的崩溃)+ "
    "cell 重建加 brief 平台兜底。"
    "(1) 根因:v0.30.12 让 secretariat 在 strategy_debate_* 期间开 adaptive "
    "thinking,但 secretariat 每轮输出完整大 plan(多 directions + matrix_"
    "skeleton),32K max_tokens 被 thinking 挤占导致 plan JSON 截断,"
    "target_platforms/matrix_skeleton 丢失,cell 重建得 0 → "
    "'工部·格子规划产出为空' RuntimeError。回退:_use_thinking 不再为 "
    "strategy_debate_* 放行 thinking(回到 v0.30.11 行为)。ministry_justice "
    "的 thinking(v0.30.12 P2-2)保留,无副作用。"
    "(2) 防御:_reconstruct_active_cells 在 plan.target_platforms 为空时退回 "
    "brief.target_platforms(crown_prince 提取的原始平台)兜底 + warning,"
    "未来任何原因的 plan 截断不再直接崩。"
    "v0.30.12 chore: Claude 模型表收敛到 4 款可用模型 + no-channel 保底 + "
    "thinking 修复。Claude 侧只剩 claude-opus-4-6 / 4-7 / 4-8 + "
    "claude-sonnet-4-6;非 Claude backend (GPT via vectorengine / DeepSeek "
    "/ Gemini) 保留原配置不动。"
    "(7) no-channel 保底:中转站对某 Claude 模型返回 'No available channel "
    "for model xxx' 时,_call_claude 按 CLAUDE_FALLBACK_CHAIN 依次降级到下一"
    "个 Claude 模型重试,而不是整个 stage 失败。实际跑通的模型经 7-tuple "
    "返回,run() 在 model_used 标 [fallback←原模型],UI/stage_log 如实反映。"
    "GPT/DeepSeek 不走这条链。"
    "(8) thinking 修复:ministry_justice 加进 THINKING_STAGES(原靠 "
    "-thinking 后缀,收敛后丢了深推理);_use_thinking 识别 strategy_debate_N "
    "前缀,让 secretariat/chancellery 在策略辩论期间不丢 thinking。"
    "(1) Claude 侧:所有 Sonnet 3.7 (claude-3-7-sonnet-*-thinking) 切到 "
    "claude-sonnet-4-6 (works_builder / vibe_rewriter / red_blue_blue / "
    "red_blue_refiner / persona_simulator);ministry_justice 的 "
    "claude-opus-4-6-thinking 去掉 -thinking 后缀。thinking 行为改由 "
    "agents/__init__.py 里 adaptive thinking JSON 参数控制 (Opus 4.6+ 自带)。"
    "(2) 非 Claude 不变:chancellery / ministry_war 仍走 gpt-5.5,"
    "persona_simulator_alt 仍走 deepseek-v4-pro (跨厂家异色彩对抗保留)。"
    "(3) SONNET_CONTENT_MODEL 从 claude-sonnet-3-7 切到 claude-sonnet-4-6。"
    "(4) COST_PER_1M_INPUT/OUTPUT 加 claude-opus-4-8 ($15/$75 同 Opus tier),"
    "删除已退役的 opus-4-1 / sonnet-3-7 / 3-7-sonnet-* 条目。"
    "(5) BaseAgent.__init__ fallback model 从 claude-sonnet-4-20250514 "
    "切到 claude-sonnet-4-6 (前者带日期后缀,中转表上不一定解析)。"
    "(6) _is_adaptive_thinking_family 检查加 claude-opus-4-8 分支。"
    "v0.30.11 历史: Gemini 按岗位 model override + 按模型计价表。"
    "(1) 新加 GEMINI_MODEL_OVERRIDES dict(pipeline/config.py),6 个 "
    "Gemini-driven agent (critic / structure_reviewer / trend_scout / "
    "image_transcriber / screenshot_analyzer / reference_analyzer)各自岗位"
    "独立挑模型;GEMINI_MODEL 退为全局默认。(2) 默认配置:vision + "
    "structure + trend → gemini-3.5-flash (2026-05 发布,agentic/"
    "multimodal 领先 3.1 Pro 且便宜 40%);critic + reference_analyzer 保 "
    "3.1 Pro(烟火气判断 + 长文稠密召回还是 Pro 强,jury still out)。"
    "(3) 新加 GEMINI_PRICE_TABLE,每模型独立费率;cost_usd 改用按模型查表"
    "(以前全局一套率)。(4) gemini_client.py 暴露 resolve_gemini_model(role) "
    "helper;6 个 agent 调用时各自传 role。(5) 设置页 → Gemini 区显示按岗位"
    "映射表 + 阶段列表显示每阶段真实分配的模型。"
    "v0.30.10 历史: strategy_loop 每次 resume 都强制重跑的老 bug — "
    "_strategy_loop 实际写 strategy_debate_N 那种 stage_log,但 line 383 "
    "的 resume 检查 done[\"secretariat\"] 永远拿不到 → 每次应用修订意见"
    "或继续执行都重跑整个策略辩论,secretariat 看到 _revision_context "
    "可能新增 direction(D6/D7),下游 cell_planner 给新 D 生成新 cell,"
    "用户感觉『一直在跑新的 D』。修复:从 strategy_debate_* 反推最后一个"
    "完整 plan(取最后一个偶数 turn = secretariat 发言的轮次,plan 在 "
    "current_plan 字段),合成 done[\"secretariat\"] 让 resume 能跳过策略层。"
    "v0.30.9 历史: 红蓝精炼真异模型对抗 + 创意/内容阶段统一改用 4.6/3.7: "
    "(1) 红蓝精炼拆 RedBlueRed (Opus 4.6) + RedBlueBlue (Sonnet 3.7),"
    "两个独立 stage_log 串行调用,Red 找 attacks 蓝队接力修复,"
    "Red 空数组就跳过蓝队省 token;(2) 创意/内容相关阶段一律 4.6 或 "
    "3.7 — vibe_critic / narrative_director / structural_rewriter / "
    "ministry_personnel 从 Opus 4.7 降到 Opus 4.6(4.7 太精致,中文短"
    "社交内容反而失真)。Opus 4.7 仅留给纯策略推理(crown_prince / "
    "secretariat / chancellery_final / ministry_works)。"
    "v0.30.8 历史: 启用 DeepSeek + 画像模拟双模型并跑: persona_simulator_alt "
    "新 stage(deepseek-v4-pro)和主 persona_simulator(Sonnet 3.7)并行,"
    "orchestrator asyncio.gather 启动两个 agent → 合并 personas 数组,"
    "每条画像加 _source 字段(claude/deepseek)。同 id 自动改名避免冲突。"
    "任一 backend 失败软降级,只缺谁的标记缺失,不阻塞流水线。"
    "Claude 偏目标用户细腻反应,DeepSeek 偏草根/破圈视角,distribution 互补。"
    "v0.30.7 历史: GPT 改走 vectorengine.ai 的 OpenAI-compat 接口: tdyun "
    "anthropic-compat 中转不支持 GPT 模型,新增独立 OpenAI SDK 后端 "
    "(api.vectorengine.ai/v1/chat/completions)。secrets.toml 加 "
    "VECTORENGINE_API_KEY,_call_claude 检测到 gpt-* 自动 dispatch 到 "
    "_call_openai_chat helper,返回元组同 _call_claude,上层无需分支。"
    "兼容 OpenAI / o1 / gpt-5 系的 max_completion_tokens 自动切换。"
    "requirements.txt 加 openai>=1.40。"
    "v0.30.6 历史: Commit B — 1 个 HIGH + 3 个 MEDIUM 流程修复(audit 后续):"
    "(M3)红蓝精炼传完整 system_prompt 而非前 500 字,Red Team 现在能"
    "看到合规块/关键词避免误改。(M4)画像模拟和消费者模拟按 cell.platform "
    "评判,多平台 brief 不再用 xhs 画像评 douyin cell;persona_simulator.md"
    "新加多平台处理段。(M1)Gemini 结构审 hint 不再被 critic-pass 吃掉:"
    "critic 让该 cell 过但 missing_items 非空时,强制 force-fail "
    "borderline 进 rewriter 补结构。(H5)策略升级后清理受影响 cell 的"
    "advisory 数据(persona_reactions/narrative_director/red_blue/"
    "consumer_simulation),终审不再读过期诊断。"
    "M2(cell_planner 跨批次共享 path 分配)留作后续单独决策。"
    "v0.30.5 历史: Commit A — 4 处 dead drop 注入修复(audit HIGH 级):"
    "(H1)工部·构建拿到 brief — slim_brief 含 target_audience / "
    "core_claim / competitive_context / _user_raw_input,builder.md "
    "新加『输入访问指南』教它何时翻原文;narrative_director rebuild "
    "也带 brief。(H2)Gemini 趋势取样的真实小红书帖子 (_trend_intel."
    "formatted_block) 显式注入 secretariat input,secretariat.md 加"
    "『趋势取样校准』段把它当第一性输入。(H3)persona_simulator 的"
    "per-cell 反应进 vibe_critic input,critic.md 加第 0.4 步『画像"
    "反应交叉校验』,3 画像 ≥2 skip 强制 borderline。(H4)叙事导演"
    "诊断 slim 摘要进 chancellery_final input,chancellery.md 加"
    "『跨 cell 一致性』必查段。"
    "v0.30.4 历史: 架构层修复: 太子不再是单点瓶颈。"
    "orchestrator 把用户原始 free_text 挂到 brief._user_raw_input,"
    "所有下游 agent 都能直接读原文(之前的 _raw_input_text 是 dead drop,"
    "零 prompt 引用)。foundation_common.md 加『用户原始输入访问协议』,"
    "统一指引下游何时翻原文 vs 信任太子。crown_prince.md 强化角色边界:"
    "保管员 + 索引制作者,不是策略分析师——产品定位/目标人群/竞品策略"
    "都是中书省/六部的活,太子只做结构化字段填充 + verbatim 保留素材。"
    "v0.30.3 历史: 太子输入两处修复: (1) 截图分析(Gemini Vision)文本现在"
    "会被 orchestrator 自动包装成 [参考文件: gemini_screenshot_analysis] "
    "块拼进 free_text,自动受 60% 硬留存规则保护——之前只挂在 brief 字段,"
    "几百字识图被太子压成一句总结;(2) 重跑时从 brief 里 strip "
    "_revision_context / strategic_warnings 等流水线内部 state,避免上一"
    "轮的修订意见污染 crown_prince 输入。用户原始信号(_screenshot_analysis* "
    "/ _reference_post_urls / _library_sample_analyses)保留。"
    "v0.30.2 历史: tdyun 中转 claude-opus-4-7-thinking 没配价导致 400,"
    "未配置定价(BadRequestError type=new_api_error)。把所有策略阶段"
    "改用 claude-opus-4-7 base(无 -thinking 后缀)— 4.7 base 内部仍"
    "做 extended reasoning,只是不接受外部 thinking budget 控制。"
    "刑部保留 claude-opus-4-6-thinking(已定价)。"
    "v0.30.1 历史: 输出中心简化 + 修订按钮智能分流提示: 输出中心顶部新增"
    "『成品提示词清单』主区,直接列 N 个不重复 prompt 的完整内容 + "
    "示例文稿(代替原来要翻平台 tab + 多层 expander 的繁琐结构)。"
    "应用修订按钮文字按实际行为校正:扫 mandatory_revisions 文本里的 "
    "D\\d+ 和全局关键词,提前告诉用户『只会重建 D5』vs『会重跑整个工部』。"
    "v0.30.0 历史: 多 vendor 路由 + 高质量模型预设(premium_multi_vendor): "
    "(1) DeepSeek 走官方 anthropic-compat 端点(api.deepseek.com/anthropic),"
    "新增 DEEPSEEK_API_KEY secret + per-model 路由器 _get_client_for_model;"
    "(2) GPT 走 tdyun 中转的 anthropic-compat 路径(model='gpt-5.5'),"
    "thinking 参数对 GPT 强制屏蔽(避免 OpenAI 后端 400);"
    "(3) 各 stage 模型映射写入 PREMIUM_MULTI_VENDOR_MAP(代码层),"
    "user 在 secrets.toml 删 model_overrides 即可启用;"
    "(4) 中书省 ↔ 门下省 故意异厂家(Claude vs GPT)避免辩论同色彩。"
    "v0.29.12 历史: (1) STAGE_MAX_TOKENS["
    "ministry_personnel] 20K→32K,多画像 × authenticity_card 字段长容易"
    "撞上限导致响应截断;(2) _try_repair_truncated_json 的 "
    "cut_points[-300:] 硬限放开——13K+ 响应最后 300 个 cut point 常常"
    "都卡在深层嵌套里,每个 candidate 都不合法,扫全部才能找到有效"
    "cut;(3) JSON 提取失败错误消息改成 first 200 + last 200 双端"
    "预览,方便判断是整段不是 JSON 还是只是尾部截断。"
    "功能同 v0.29.11(画像模拟接入反馈链): 之前 persona_simulator 只写 "
    "_persona_reactions 给 UI 显示,不参与任何决策,跑了等于白烧 token。"
    "现在每条 cell 扫 3 个画像的 action,全 skip 的 cell 追加进 "
    "strategic_warnings,和 consumer_simulation 走同一条告警通道 —— "
    "UI 红色警告 + 如果 ENABLE_STRATEGIC_ESCALATION 开启会触发 "
    "secretariat 修订 direction 的 stop_trigger/reward_type。"
    "不依赖 summary.weak_cells(模型有时给 direction_id 而非 cell_id),"
    "直接从 personas[*].reactions 逐 cell 统计更稳。"
    "功能同 v0.29.9(流水线详情页可观测性大升级): (1) 新增『中间精炼』tab 展示 "
    "叙事导演 / 红蓝精炼 / 画像模拟 三个阶段,之前 UI 没位置、"
    "图标染色点进去看不到内容; (2) 网感 tab 补上 叙事结构重写 "
    "(structural_rewriter) 的 per-cell 摘要; (3) 太子 tab 顶部新增 "
    "『📎 接收到的参考文件』清单,按 txt/md/pdf/docx/图片 统一识别 "
    "[参考文件: name] 包装 + 从 body 推断 kind/status,每个文件"
    "一行状态图标 + 字数 + 预览,不再需要翻几百行 base64 确认"
    "收到没;(4) free_text 显示一律折叠 BASE64_IMAGE 块和裸 "
    "base64 串,用『📎 已折叠 · N 字符』代替,实际喂 agent 的是"
    "完整原文不受影响。"
)

# ── Model assignments per stage ────────────────────────────────────────────
# v0.32.0 起主链路只剩两家:Kimi (Moonshot) 和 DeepSeek。两家都提供
# Anthropic-compatible 端点,所以整条调用链仍然走 anthropic SDK,
# agents/__init__.py 按模型名前缀路由到各自 base_url:
#
#   kimi-*      → https://api.moonshot.cn/anthropic   (MOONSHOT_API_KEY)
#   deepseek-*  → https://api.deepseek.com/anthropic  (DEEPSEEK_API_KEY)
#
# thinking 的开关仍由 THINKING_STAGES 控制,但参数形态按厂家分流
# (见 agents/__init__.py::_call_model):Kimi 收 {"type":"adaptive"},
# DeepSeek 端点会静默忽略 thinking 字段所以干脆不发。
#
# ── 三档模型 ─────────────────────────────────────────────────────────
# 价格来源:Moonshot / DeepSeek 官网 2026-08 公开价,单位 USD / 1M token。
# 换算下来整条流水线比换厂前(Opus 4.7 $15/$75)便宜约一个数量级。

# 旗舰档 — 2.8T MoE / 1M 上下文 / $3 in · $15 out。只给"错了下游全废"
# 的决策上游用,不铺开。
FLAGSHIP_MODEL = "kimi-k3"
# 主力档 — 262K 上下文 / 原生多模态 / $0.95 in · $4 out。中文写作和网感
# 判断是这一档的强项,所以内容生成、网感judge、画像创作全在这里。
PRIMARY_MODEL = "kimi-k2.6"
# 廉价异厂家档 — $0.14 in · $0.28 out。用途有两个:(1) 给辩论/对抗环节
# 提供跟 Kimi 不同 distribution 的第二方视角;(2) 高频低难度的批量判断。
CHEAP_MODEL = "deepseek-v4-flash"

# ── 向后兼容别名 ────────────────────────────────────────────────────
# v0.31 及之前这三个常量是 Claude 名字。外部脚本 / 历史 preset 可能还在
# import 它们,保留别名指向新档位,避免 ImportError。语义已经不是
# "Opus/Sonnet" 而是 "旗舰/主力/内容",新代码请直接用上面三个。
OPUS_MODEL = FLAGSHIP_MODEL
SONNET_MODEL = PRIMARY_MODEL
SONNET_CONTENT_MODEL = PRIMARY_MODEL

# ── MODEL_PRESET options ─────────────────────────────────────────────
#   "kimi_deepseek"(v0.32.0 默认,推荐)— 按 KIMI_DEEPSEEK_MAP 逐 stage
#       精确分配。策略核心 K3、对抗环节 DeepSeek、其余 K2.6。
#   "all_primary"  — 全部 kimi-k2.6。最省事,策略深度会掉一档,适合
#       跑通验证 / 成本敏感的试点。
#   "all_flagship" — 全部 kimi-k3。最贵最深,适合单次高价值交付。
#   "premium_multi_vendor" / "content_sonnet" / "all_opus" / "all_sonnet"
#       — v0.31 及之前的旧名,保留兼容:前者等价 kimi_deepseek,
#         后三者等价 all_primary(它们原本的 Opus/Sonnet 语义已不存在)。
MODEL_PRESET = "kimi_deepseek"

# 各阶段精确模型映射 — 写在代码里防止 secrets.toml 误覆盖。
KIMI_DEEPSEEK_MAP: dict[str, str] = {
    # ── 策略核心 4 个:kimi-k3 ────────────────────────────────────────
    # 判定标准是"这一步错了,下游全部作废":太子丢素材 → 所有人看不到原始
    # 信号;中书省定错方向 → 六部全在错方向上精耕;工部架构错 → 每个 cell
    # 都长歪;终审放行 → 直接出货给客户。只有这 4 个值 3 倍单价。
    "crown_prince": FLAGSHIP_MODEL,                # 太子(整理 + 索引)
    "secretariat": FLAGSHIP_MODEL,                 # 中书省(策略发言)
    "ministry_works": FLAGSHIP_MODEL,              # 工部架构(整脊柱)
    "chancellery_final": FLAGSHIP_MODEL,           # 终审(holistic 把关)
    # ── 异厂家对抗:deepseek-v4-flash ─────────────────────────────────
    # 换厂前这两个跑 gpt-5.5,目的是"别让辩论双方同色彩自言自语"。现在
    # 中书省(Kimi)↔ 门下省(DeepSeek)仍然是跨厂家,对抗性保留;兵部的
    # 刁钻竞争视角同理。顺带 OpenAI 依赖整条去掉。
    "chancellery": CHEAP_MODEL,                    # 门下省(critic)
    "ministry_war": CHEAP_MODEL,                   # 兵部(刁钻竞争)
    # ── 结构化派发 / 五部:kimi-k2.6 ──────────────────────────────────
    "dispatcher": PRIMARY_MODEL,
    "ministry_revenue": PRIMARY_MODEL,
    "ministry_rites": PRIMARY_MODEL,
    "ministry_justice": PRIMARY_MODEL,             # 合规审(带 thinking,见 THINKING_STAGES)
    "ministry_works_cell_planner": PRIMARY_MODEL,
    # ── 创意 / 内容:kimi-k2.6 ────────────────────────────────────────
    # K2.6 的中文写作和"人味"是选它的主要理由,内容侧不降档。
    "ministry_personnel": PRIMARY_MODEL,           # 画像创作(创意)
    "narrative_director": PRIMARY_MODEL,           # 跨 cell 一致性诊断
    "vibe_critic": PRIMARY_MODEL,                  # 网感复检(judge)
    "structural_rewriter": PRIMARY_MODEL,          # 身份/缺口手术
    "ministry_works_builder": PRIMARY_MODEL,       # 内容写作
    "vibe_rewriter": PRIMARY_MODEL,                # 内容重写
    # ── 红蓝精炼:攻方 Kimi vs 守方 DeepSeek ──────────────────────────
    # 换厂前是 Opus(攻) vs Sonnet(守),同厂异档。现在直接做成跨厂:
    # 攻方 K2.6 找 AI 腔指纹,守方 DeepSeek 接力做最小修复,两个
    # distribution 差异比同厂异档更大,红队更不容易"自己放过自己"。
    "red_blue_red": PRIMARY_MODEL,                 # 攻方
    "red_blue_blue": CHEAP_MODEL,                  # 守方
    "red_blue_refiner": PRIMARY_MODEL,             # legacy 兼容,实际不用
    # ── 画像模拟双 backend:主 Kimi + alt DeepSeek ────────────────────
    # v0.30.8 起就是双跑合并 personas[]。alt 从 deepseek-v4-pro 降到
    # v4-flash,和主链路其它 DeepSeek 用量对齐(画像模拟是高频批量判断,
    # 不需要 pro 档;异厂家视角来自"是 DeepSeek"而不是"是 pro")。
    "persona_simulator": PRIMARY_MODEL,            # 主路径
    "persona_simulator_alt": CHEAP_MODEL,          # DeepSeek 异厂家
}

# v0.31 及之前的名字,保留供外部 import。
PREMIUM_MULTI_VENDOR_MAP = KIMI_DEEPSEEK_MAP

_STAGE_ROLES = {
    # Strategy / review: needs reasoning depth
    "crown_prince": "strategy",
    "secretariat": "strategy",
    "chancellery": "strategy",
    "chancellery_final": "strategy",
    "ministry_works": "strategy",
    # Structured planning: Opus preferred for stability
    "dispatcher": "planning",
    "ministry_personnel": "planning",
    "ministry_revenue": "planning",
    "ministry_rites": "planning",
    "ministry_war": "planning",
    "ministry_justice": "planning",
    "ministry_works_cell_planner": "planning",
    # Cross-cell coherence: needs reasoning (sees whole matrix)
    "narrative_director": "strategy",
    # Content generation + taste judgment: voice quality matters
    "ministry_works_builder": "content",
    "red_blue_refiner": "content",     # legacy(v0.30.9 拆分后基本不用)
    "red_blue_red": "content",          # v0.30.9: 红蓝攻方
    "red_blue_blue": "content",         # v0.30.9: 红蓝守方
    "persona_simulator": "content",    # simulates real humans (Kimi 系)
    "persona_simulator_alt": "content",  # v0.30.8: DeepSeek 异厂家画像
    "vibe_critic": "content",
    "vibe_rewriter": "content",
    # v0.29.0: 叙事结构重写者 — 和 vibe_rewriter 同角色(内容写作)。
    "structural_rewriter": "content",
}
# ⚠️ v0.32.0 起 role 标签不再参与默认 preset 的模型选择(kimi_deepseek 逐
# stage 精确指定)。这个 dict 现在的作用是:(1) 定义"合法 stage 名"的全集,
# MODELS 的 key 从这里来;(2) all_primary / all_flagship 单档 preset 的遍历
# 依据。新增 stage 时仍然要在这里登记,否则 MODELS 里没有它、BaseAgent 会
# 落到构造函数的兜底模型。


def _resolve_models(preset: str) -> dict[str, str]:
    """Assemble the MODELS dict from role tags + preset. Returning a dict
    keeps consumers (logging, cost accounting, settings UI) unchanged.

    kimi_deepseek(v0.32.0 默认):
      每个 stage 直接从 KIMI_DEEPSEEK_MAP 取。没在 map 里的 stage(罕见,
      通常是新增 stage 还没补)fallback 到 PRIMARY_MODEL —— 既保证能跑,
      也不会因为漏配就悄悄用上最贵的旗舰档。

    all_primary / all_flagship:
      全链路单档,分别是 kimi-k2.6 / kimi-k3。

    旧 preset 名(premium_multi_vendor / content_sonnet / all_opus /
    all_sonnet)在 v0.32.0 换厂后已无对应语义,映射到最接近的新档位。
    """
    if preset in ("kimi_deepseek", "premium_multi_vendor"):
        return {
            k: KIMI_DEEPSEEK_MAP.get(k, PRIMARY_MODEL)
            for k in _STAGE_ROLES
        }
    if preset == "all_flagship":
        return {k: FLAGSHIP_MODEL for k in _STAGE_ROLES}
    # all_primary + 所有旧的 Claude 档位名 → 全 K2.6
    return {k: PRIMARY_MODEL for k in _STAGE_ROLES}


MODELS: dict[str, str] = _resolve_models(MODEL_PRESET)

# ── Model fallback chain (无可用渠道时的保底) ─────────────────────────────
# 中转 / 厂商偶尔对某个模型返回 "No available channel for model xxx"(该模型
# 当前没有可用上游渠道),或短暂下线。这不是模型本身的问题,换一个模型通常
# 就能跑。_call_model 检测到这类错误时按下面顺序降级,避免整个 stage 失败。
#
# 顺序是"能跑通的概率 × 质量"的折中:主力 K2.6 → 旗舰 K3 → 跨厂家保底
# DeepSeek。最后一档故意跨到另一家:Moonshot 整体故障时,DeepSeek 仍能让
# 流水线以降级质量跑完,而不是整条 run 挂掉。
#
# ⚠️ 降级会跨厂家,所以 agents/__init__.py 里的 fallback 循环必须跳过
# 「backend 没配 key」的候选 —— 否则 "DEEPSEEK_API_KEY 没配" 这种配置错误
# 会被当成 no-channel,在链上一路重试后报一个和真实原因无关的错。
MODEL_FALLBACK_CHAIN: list[str] = [
    PRIMARY_MODEL,
    FLAGSHIP_MODEL,
    CHEAP_MODEL,
]

# v0.31 及之前的名字,保留供外部 import。
CLAUDE_FALLBACK_CHAIN = MODEL_FALLBACK_CHAIN

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
# Exponential backoff base. Delay for attempt N is
# RETRY_BASE_DELAY_SECONDS * 2**N (so 3s after attempt 0, 6s after attempt
# 1, 12s after attempt 2, ...). With MAX_RETRIES=2 this matches the old
# linear 3,6 sequence; the formula is kept exponential so bumping
# MAX_RETRIES doesn't silently change the retry curve.
RETRY_BASE_DELAY_SECONDS = 3

# ── Clarification ─────────────────────────────────────────────────────────
# How long the pipeline waits for the user to answer a clarification
# request before giving up. 1 hour is generous for humans to come back
# from another tab / lunch, short enough that a truly-abandoned run gets
# cleaned up eventually.
CLARIFICATION_TIMEOUT_SECONDS = 3600
# Poll interval while waiting for user response.
CLARIFICATION_POLL_SECONDS = 5
# Max clarification rounds per agent — if the model keeps asking, force
# continue with whatever partial output it gave instead of looping forever.
MAX_CLARIFICATION_PER_AGENT = 2

# ── Platform demo length bounds ──────────────────────────────────────────
# (min_chars, max_chars) for demo_output validation per platform. Keys are
# matched via substring+case-insensitive against the cell's platform field,
# so both Chinese and romanized variants resolve to the same range. Values
# are approximate — the validator allows 1.5× the max as a hard ceiling.
PLATFORM_DEMO_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "小红书": (200, 1000),
    "xiaohongshu": (200, 1000),
    "抖音": (50, 500),
    "douyin": (50, 500),
    "b站": (150, 600),
    "bilibili": (150, 600),
    "知乎": (300, 2000),
    "zhihu": (300, 2000),
    "微博": (20, 200),
    "weibo": (20, 200),
}
# Fallback when the platform isn't recognized.
PLATFORM_DEMO_LENGTH_DEFAULT: tuple[int, int] = (50, 2000)

# ── Platform voice markers（平台调性指纹）─────────────────────────────────
# 每个平台**独有**的调性词。用途:检测 works_builder 把平台调性段写串了 ——
# 小红书的 cell 里写着「前3秒钩子」(那是抖音的),或知乎 cell 写着「姐妹安利感」。
# 词表出处是 works_builder.md 规则 6「平台口吻细节」。
#
# ⚠️ 只收**该平台独有**的词,不收跨平台通用词。反例:「钩子」五个平台都在用,
# 收进来会让所有 cell 互相误报;「姐妹们」出现在 works_builder 的禁止开场
# 清单里,任何平台的 system_prompt 都可能带它。选词标准是"看到这个词就能
# 断定在讲哪个平台"。
#
# 判定规则见 quality_metrics.check_platform_voice —— 只有「自己平台的词一个
# 没有 + 别家平台的词命中 ≥2 个」才报,单个外来词不报(很可能是"不要写成抖音
# 那种前3秒"这类反例引用)。这是有意压低召回换精度:本仓库 v0.32.3/v0.32.4
# 两次三轮空烧都是误判造成的。
PLATFORM_VOICE_MARKERS: dict[str, tuple[str, ...]] = {
    "小红书": ("姐妹安利", "安利感", "生活流", "碎片感", "绝绝子", "真的会谢",
               "无语子", "yyds", "笔记正文", "收藏率"),
    "抖音": ("前3秒", "前 3 秒", "头三秒", "完播", "口播", "分镜", "运镜",
             "第一帧", "脚本时长"),
    "b站": ("弹幕", "UP主", "up主", "三连", "钻研感", "我研究过", "我试过",
            "视频简介"),
    "知乎": ("谢邀", "答主", "高赞", "观点先行", "先说结论", "论据", "回答正文"),
    "微博": ("超话", "转评赞", "热搜", "话题感", "@", "博文"),
}
# 平台名归一化:cell.platform 可能是中文名也可能是罗马字。和
# PLATFORM_DEMO_LENGTH_RANGES 的 substring 匹配思路一致。
PLATFORM_VOICE_ALIASES: dict[str, str] = {
    "小红书": "小红书", "xiaohongshu": "小红书", "xhs": "小红书", "red": "小红书",
    "抖音": "抖音", "douyin": "抖音", "tiktok": "抖音",
    "b站": "b站", "bilibili": "b站", "哔哩": "b站",
    "知乎": "知乎", "zhihu": "知乎",
    "微博": "微博", "weibo": "微博",
}

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # plan_review: force pass on round 3 (legacy, used by non-debate path)

# ── Strategy Debate ──────────────────────────────────────────────────────
# Max turns in the secretariat ↔ chancellery multi-turn debate.
# Secretariat speaks on even turns, chancellery on odd. So MAX_DEBATE_TURNS=8
# means 4 exchanges (each agent speaks 4 times). Chancellery can approve
# at any odd turn to end early. Last chancellery turn is force-approve.
# ⚠️ v0.33.2: 8 → 4(即 2 个来回)。
#
# 这是全流水线最贵的非格子环节:中书省每一轮都要重吐**完整大 plan**
# (5-7 个 tactical_directions + 十几个 active_cells 的 matrix_skeleton),
# 跑 kimi-k3 的 $15/1M 输出档,而且正是它把 MAX_TOKENS_STRATEGY 逼到 48000 的
# (见那个常量上方记的两次截断事故)。8 轮 = 中书省吐 4 次完整 plan。
#
# 砍到 4 的理由不是"省钱",是**对抗性本来就是跛的**:config 自己在
# THINKING_STAGES 那里承认,门下省现在跑 deepseek-v4-flash,而 DeepSeek 端点
# 不认 thinking 参数,所以门下省【实际上没有 extended thinking】。
# 于是这场辩论是「k3 深推理提案」对「廉价档浅判断」—— 第 3、4 个来回能挑出
# 前两个来回没挑出的东西的概率很低,但成本是线性翻倍的。
#
# ⚠️ v0.33.6 修正:4 → 6。砍到 4 是错的,代价被低估了。
#
# 轮次的实际语义是「偶数轮=中书省、奇数轮=门下省」,而末轮门下省会强制放行:
#   4 轮 → 提案(0) 审议(1) 修订(2) 强制放行(3)
#          = 只有一次真实审议,**修订后的方案从来没被审过**
#   6 轮 → 提案(0) 审议(1) 修订(2) 审议(3) 修订(4) 强制放行(5)
#          = 修订版拿到一次真实复核
#
# 而修订恰恰是最需要复核的那一版 —— 它是按上一轮质疑改出来的,改对没改对
# 只有再审一次才知道。省那两轮省掉的正是这条链上最有价值的一次判断。
#
# 配套改动(见 _strategy_loop):末轮不再"凭空合成一个 approved",而是**照常
# 调用门下省**、拿到质疑后再强制放行并把未解决的质疑写进 overall_assessment。
# 多花一次 deepseek-v4-flash(最便宜的一档),换回"交付的方案一定被对抗性
# 看过至少一遍"。
#
# 6 相对原来的 8 仍然省一轮完整往返(中书省每轮要重吐完整大 plan、跑 k3 的
# $15/1M 输出档,那是这条链上最贵的部分)。
MAX_DEBATE_TURNS = 6

# final_review (工部产出的 prompt_matrix) 的轮次上限。第一次跑流水线 = round 1；
# 用户每点一次「应用修订意见并重跑」round +1。超过 MAX_FINAL_REJECTIONS 后强制
# 放行，并在 suggestions 里打风险注。防止终审无限驳回工部造成死循环。
MAX_FINAL_REJECTIONS = 3

# ── Token limits ───────────────────────────────────────────────────────────
# max_tokens must accommodate (thinking_budget + actual_output) for thinking stages.

MAX_TOKENS_DEFAULT = 16000
# ⚠️ v0.32.3: 32000 → 48000。这是在拆一颗换厂后被重新装上的雷。
#
# 历史:v0.30.12 给 secretariat 在策略辩论期间开了 thinking,结果 plan JSON 被
# 截断 —— thinking 和正文共享同一个 max_tokens 预算,而 secretariat 每轮要吐
# 完整大 plan(5-7 个 tactical_directions + 十几个 active_cells 的
# matrix_skeleton)。截断后 target_platforms / matrix_skeleton 丢失,cell 重建
# 得 0 个 → "工部·格子规划产出为空" 直接崩。v0.30.13 的修法是【关掉】辩论期
# 的 thinking,把 32K 全留给输出。
#
# 换厂后这颗雷被重新装上:secretariat / crown_prince / ministry_works /
# chancellery_final 都在 THINKING_STAGES 里,现在跑 kimi-k3 且走
# {"type":"adaptive"} —— Moonshot 是否把 thinking 计入 max_tokens 没有明确
# 文档,如果计入,32K 又会被挤占,同一个崩溃模式复现。
#
# 与其赌它不计入,不如把天花板抬到 48K:K2.6/K3 的输出上限远高于此(K2.6 标称
# 262144),max_tokens 只是【上限】不是预付费,没用到就不花钱。而截断的代价是
# 三轮重试甚至整条 run 崩 —— 两边期望值差得很远。
#
# 不抬得更高是因为上限太松会让模型倾向写更长的 plan,反而拖慢下游;48K 相对
# 32K 已经给 thinking 留出足够余量。
MAX_TOKENS_STRATEGY = 48000  # strategy/review stages need most room

STAGE_MAX_TOKENS: dict[str, int] = {
    "crown_prince": MAX_TOKENS_STRATEGY,
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "dispatcher": 20000,
    # v0.29.12: 吏部经常超 20K(多画像 × authenticity_card 字段长),撞
    # max_tokens 截断产出破损 JSON。修复 JSON 重建能救大多数,但直接
    # 给足上限从源头减少截断。
    "ministry_personnel": 32000,
    "ministry_revenue": 20000,
    "persona_simulator_alt": 20000,  # v0.30.8: 和主 persona_simulator 同档
    "red_blue_red": 16000,             # v0.30.9: 攻方输出 attacks 列表,不需要太大
    "red_blue_blue": 24000,            # v0.30.9: 守方输出 fixes + refined demo + system_prompt
    "ministry_rites": 20000,
    "ministry_war": 20000,
    "ministry_justice": 20000,
    "ministry_works": MAX_TOKENS_STRATEGY,
    "ministry_works_cell_planner": 20000,
    "ministry_works_builder": 32000,
    "vibe_critic": 20000,
    "vibe_rewriter": 24000,
    "structural_rewriter": 24000,  # v0.29.0: 和 vibe_rewriter 一致
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
MATRIX_BATCH_CONCURRENCY = 5    # parallel builder calls
MATRIX_CELLS_PER_BATCH = 1      # one cell per call (safest for JSON structure)

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 5    # parallel cell-planner calls

# ── 8 条突破路径的跨批次分配(v0.33.0,原 M2 遗留项)────────────────────────
# 出处:works_cell_planner.md「8 条突破路径」。每个 cell_plan 要显式选 2-3 条。
#
# 问题:cell_planner 是**分批并行**跑的(BATCH_SIZE=5,CONCURRENCY=5),批次之间
# 互不知道对方选了什么路径。12 个格子分 3 批同时跑,三批各自挑路径,撞车是必然
# 且不可见的 —— 而路径组合正是"这几个方向的打法不重样"的核心机制。
# config.py 的 v0.30.6 版本注记里记着「M2(cell_planner 跨批次共享 path 分配)
# 留作后续单独决策」,这就是那个决策。
#
# 解法:orchestrator 在分批**之前**按 direction_id 确定性预分配,把结果塞进每个
# 批次的输入。零 LLM 成本,且跨批次一致性由代码保证而不是靠模型自觉。
#
# 为什么按 direction 而不是按 cell 分配:矩阵是 方向 × 平台。需要不重样的是
# **方向之间**(D1 和 D2 是两种打法);同一方向的不同平台(D1_xhs / D1_douyin)
# 是同一种打法的平台适配,本来就该共用同一组路径。
PATH_LIBRARY: tuple[str, ...] = (
    "身份路径", "对象路径", "语言路径", "结构路径",
    "评价路径", "场景路径", "信息差路径", "情绪路径",
)
# 第 i 个方向拿 PATH_LIBRARY[(i + off) % 8] for off in PATH_ROTATION_OFFSETS。
# 3 和 5 都与 8 互质,所以相邻方向拿到的三元组不会有重叠模式,整体循环周期是 8
# —— 方向数 ≤8 时保证任意两个方向的路径组合都不同。
PATH_ROTATION_OFFSETS: tuple[int, ...] = (0, 3, 5)

# ── Extended Thinking ─────────────────────────────────────────────────────
# 6 strategy/review/compliance stages use extended thinking (budget_tokens
# on relay, adaptive on Vertex/4.6+). Execution stages skip thinking for speed.
#
# v0.30.12: ministry_justice 加入。历史上它靠模型名后缀
# claude-opus-4-6-thinking 拿 extended thinking;当可用模型表收敛到 base
# 模型(无 -thinking 变体)后,后缀路径失效。改为走 THINKING_STAGES +
# adaptive thinking JSON 参数,保证合规审查仍有深推理(安全/法务把关不能
# 悄悄变弱)。

# ⚠️ v0.32.0: chancellery(门下省)留在这个集合里,但它现在跑 deepseek-v4-flash,
# 而 DeepSeek 的 anthropic-compat 端点不认 thinking 参数 —— 所以它【实际上没有】
# extended thinking,stage_log 会如实显示 thinking ✗。
# 这是明知的取舍:门下省的价值主要来自"跟中书省不同厂家、不同 distribution"的
# 对抗性,而不是推理深度;深推理那一侧由中书省的 kimi-k3 承担。哪天想换回来,
# 把 KIMI_DEEPSEEK_MAP["chancellery"] 改成 PRIMARY_MODEL/FLAGSHIP_MODEL 即可,
# thinking 会自动恢复(_call_model 按厂家判断,不需要动这个集合)。
THINKING_STAGES: frozenset[str] = frozenset({
    "crown_prince",
    "secretariat",
    "chancellery",
    "ministry_works",
    "chancellery_final",
    "ministry_justice",   # v0.30.12: 合规审查,接替原 -thinking 后缀
})

THINKING_BUDGET_TOKENS = 10000  # for relay mode (budget_tokens)

# ── Rate Limiting ─────────────────────────────────────────────────────────
# Sliding-window rate limiter. The active window respects two constraints
# simultaneously:
#
#   1. RPM cap (sustained):    at most CLAUDE_RPM_LIMIT call STARTS per
#                              rolling 60-second window. When the window
#                              fills, the next call sleeps until the
#                              oldest entry ages out.
#   2. Concurrency cap (peak): at most CLAUDE_MAX_CONCURRENT calls in
#                              flight at the same instant.
#
# Tune these to your backend's published limits. Set CLAUDE_RPM_LIMIT to 0
# to disable the rate cap entirely.
#
# ⚠️ v0.32.0: 常量名里的 CLAUDE_ 是历史包袱(改名要动限流器 + 设置页 +
# 多处 preset 字段,收益不抵风险)。它管的是【主链路上游】的速率,现在指
# Moonshot / DeepSeek。两家的实际配额跟原来的 Claude 中转不是一回事,
# 15 RPM 这个 floor 多半偏保守 —— 下面的自适应安全阀会从 CEILING 起跑,
# 撞到 429 才回落,所以不用急着手调;真嫌慢就抬 CLAUDE_RPM_CEILING。
#
# Vertex AI mode bypasses this limiter entirely — Vertex enforces quota
# server-side and returns 429 we'd just retry into. See
# agents/__init__.py::_get_active_limiter.
CLAUDE_RPM_LIMIT = 15
CLAUDE_MAX_CONCURRENT = 16

# ── Adaptive RPM safety valve ────────────────────────────────────────────
# CLAUDE_RPM_LIMIT above is the KNOWN-SAFE floor. When adaptive mode is on,
# the limiter starts optimistically at CLAUDE_RPM_CEILING and only backs off
# toward the floor when the relay actually returns rate-limit (429) errors —
# then slowly probes back up. This means: if the account can sustain more
# than 15/min we get the speed for free; if it can't, we auto-fall-back to
# the safe floor so throttling stays the *relay's* problem, never a flood we
# created. Set CLAUDE_RPM_ADAPTIVE=False to pin the rate at CLAUDE_RPM_LIMIT.
CLAUDE_RPM_ADAPTIVE = True
# Optimistic ceiling to probe toward. Never sends more than this per minute.
CLAUDE_RPM_CEILING = 60
# On each observed 429/rate-limit, multiply the effective RPM by this
# (bounded below by the floor). 0.5 = halve on every throttle signal.
CLAUDE_RPM_BACKOFF_FACTOR = 0.5
# After this many seconds with no new 429, step the effective RPM up by
# CLAUDE_RPM_RECOVERY_STEP (probing back toward the ceiling).
CLAUDE_RPM_RECOVERY_SECONDS = 45
CLAUDE_RPM_RECOVERY_STEP = 3

# ── Per-run budget ───────────────────────────────────────────────────────
# Hard ceiling on combined input + output tokens accumulated within a
# single pipeline run. Once exceeded, the next agent call raises
# RunBudgetExceededError and the orchestrator marks the run as failed.
# Safety net against runaway retry loops — 14 stages × worst-case retries
# × thinking budgets can compound quickly without a cap.
#
# ⚠️ v0.32.1: 2M → 8M。v0.32.0 换厂时我把这个值留着没动,理由是"它是挡重试
# 风暴的,不是成本档位"。这个判断是错的 —— 它一直都是【成本容忍度】旋钮
# (v0.31 的原注释写的是 "Tune to your cost tolerance",2M 在 Opus 4.7
# 单价下 ≈ $74/run)。换厂后单价掉了一个数量级,同一个 2M 只剩 ≈ $5,
# 等于在没人决定的情况下把成本容忍度砍到了原来的 1/14。
#
# 后果是真实发生的:一条 8 方向 × 2 平台 = 16 格子的正常 run,在工部构建
# 跑到第 14 个格子时撞上 2M 被强杀 —— 而那时它只花了大约 $5。
#
# 8M 的算法(按实测的 in/out 比例 ≈ 64/36 折算):
#   8M ≈ 5.1M input + 2.9M output
#   全 kimi-k2.6: 5.1×$0.95 + 2.9×$4  ≈ $16
#   混 k3 策略层:                      ≈ $20–25
# 仍然低于换厂前 2M 所代表的 ≈ $74,但足够跑完 16 格子的完整流水线
# (构建 + 红蓝 + 画像 + 网感循环最多 3 轮 + 终审)。
#
# 它挡的是【重试失控】:单个 stage 反复重试、或格子数被策略升级刷到失控。
# 如果你的 run 经常撞 8M,别急着再调高 —— 先去看格子数是不是不合理
# (格子数 = 方向数 × 平台数,见 secretariat.md 的矩阵骨架段),
# 那才是成本的乘数项。
MAX_TOKENS_PER_RUN = 8_000_000

# ── Liveness: heartbeat + stale-run reaper ───────────────────────────────
# The pipeline runs in a daemon thread inside the Streamlit process. When
# Streamlit Cloud recycles/sleeps the process (SIGKILL), that thread dies
# mid-run and its except/finally never runs — the DB row stays 'running'
# forever ("zombie"). Defense: the thread writes heartbeat_at every
# HEARTBEAT_INTERVAL; a reaper on app load marks any 'running' run whose
# heartbeat is older than RUN_STALE_SECONDS as failed. Heartbeat ticks
# independently of stage progress, so a healthy long stage never looks
# dead — only a truly dead process stops the beat.
PIPELINE_HEARTBEAT_INTERVAL_SECONDS = 10
# A run is considered dead if its heartbeat hasn't advanced in this long.
# Must be comfortably larger than the heartbeat interval (missed a few
# beats = dead), but small enough that zombies clear quickly.
RUN_STALE_SECONDS = 75

# ── Kimi auxiliary assist (二审 / 结构审 / 视觉) ────────────────────────────
# v0.32.0: 这一层原本是 Gemini(google-genai + Vertex Express key),现在整体
# 迁到 Kimi。它跟主链路共用 Moonshot 的 key 和端点,但走一条独立的轻量调用
# 路径(pipeline/agents/kimi_client.py):不进 stage_log 的重试/预算体系、
# 不参与 run 级 token 熔断、失败一律降级不阻塞。
#
# 四个岗位:
#   1. critic — 网感二审(分歧仲裁)。主链路 vibe_critic 判 pass 的 cell 再
#      过一遍;这里判 fail 就打回 vibe_rewriter。存在意义是抓"主 critic 给
#      面子分"的 AI 腔产出。
#   2. structure_reviewer — 结构审。审查每个 prompt_cell 的完整性(5 个池 /
#      画像嵌入 / 合规块 / 关键词表),输出作为 advisory 追加到
#      _revision_directives。
#   3. image_transcriber — 图片预转写。用户上传的图转成文字进 brief。
#   4. screenshot_analyzer — 截图分析(小红书截图等)。
#
# 为什么二审仍然值得跑(同厂家了还审个什么?):二审的价值主要来自"换一次
# 独立采样 + 换一套 prompt 视角",而不只是换厂家。不过跨厂家确实更强,所以
# 默认把 critic 岗位钉在 DeepSeek(见 KIMI_ASSIST_MODEL_OVERRIDES),
# 保留跨厂家仲裁;其余三个纯感知/清单任务走 Kimi。
#
# Failure mode: advisory-only,调用出错只记 warning,流水线照常走完。
#
# Auth: MOONSHOT_API_KEY(主链路同一个 key)。DeepSeek 岗位用 DEEPSEEK_API_KEY。
ENABLE_KIMI_ASSIST = True

# 全局默认 — 任何没在 KIMI_ASSIST_MODEL_OVERRIDES 里钉死的岗位用这个。
KIMI_ASSIST_MODEL = PRIMARY_MODEL

# 按岗位钉模型。kimi_client.resolve_assist_model(role) 先查这里,查不到用
# KIMI_ASSIST_MODEL。
#
#   - critic:            钉 DeepSeek,保住"二审跟主判不同厂家"这条性质。
#                        主链路 vibe_critic 是 kimi-k2.6,二审用同一个模型
#                        等于自己复核自己,分歧仲裁就没意义了。
#   - structure_reviewer: 纯清单核对,K2.6 足够,也不需要跨厂家。
#   - image_transcriber / screenshot_analyzer: 必须多模态 → K2.6
#                        (DeepSeek 这两档没有视觉输入,不能钉过去)。
KIMI_ASSIST_MODEL_OVERRIDES: dict[str, str] = {
    "critic":              CHEAP_MODEL,
    "structure_reviewer":  PRIMARY_MODEL,
    "image_transcriber":   PRIMARY_MODEL,
    "screenshot_analyzer": PRIMARY_MODEL,
}

# ── 向后兼容别名 ────────────────────────────────────────────────────
# v0.31 的 Gemini 常量名。留着是为了让任何还没改到的 import 不炸,值已经
# 指向 Kimi。新代码请用上面的 KIMI_* 名字。
ENABLE_GEMINI_ASSIST = ENABLE_KIMI_ASSIST
GEMINI_MODEL = KIMI_ASSIST_MODEL
GEMINI_MODEL_OVERRIDES = KIMI_ASSIST_MODEL_OVERRIDES

# ── SocialDataX trend scout ────────────────────────────────────────────────
# The trend scout pulls real current 小红书 posts as calibration samples for
# the copy pipeline. As of this migration it fetches them via SocialDataX's
# first-party MCP (direct XHS access), replacing the old Gemini +
# Google-Search-grounding path (which could only reach XHS through Google's
# thin index, returned copyright-filtered snippets with no engagement data,
# and was disabled by default). See:
#   pipeline/agents/socialdatax_client.py
#   pipeline/agents/socialdatax_trend_scout.py
#
# Used in two places:
#   - PRE: before secretariat, to enrich the brief with real current
#          爆款 samples (原文 + 互动量) so strategy is calibrated against
#          concrete, currently-viral examples.
#   - POST: after chancellery_final, per direction, for side-by-side
#           comparison with our produced demos. Advisory, non-blocking.
#
# Auth: top-level `SOCIALDATAX_API_KEY` in .streamlit/secrets.toml (request
# one at https://socialdatax.com/?from=npm). Missing key → scout returns
# verdict="skipped" and the pipeline proceeds (advisory-only, never blocks).
#
# Cost: SocialDataX bills per API call by plan. PRE is ~1 call/run; POST is
# ~1 call per tactical direction (~5-8/run). Set SOCIALDATAX_COST_PER_CALL_USD
# to your plan's per-call price to fold it into run cost accounting.

# Master switch for all SocialDataX access (client-level). False → every
# SocialDataX call short-circuits to SocialDataXNotConfigured.
ENABLE_SOCIALDATAX = True

# MCP endpoint base. Per-platform URL is f"{base}/{platform}/mcp"
# (e.g. https://mcp.socialdatax.com/xhs/mcp). Override only if SocialDataX
# publishes a new host.
SOCIALDATAX_MCP_BASE = "https://mcp.socialdatax.com"

# Per-call network timeout (seconds). XHS scraping upstream can be slow;
# 60s is a comfortable ceiling for a single search call.
SOCIALDATAX_REQUEST_TIMEOUT_SECONDS = 60

# Search sort for the scout. like_count_descending surfaces real 爆款
# (most-liked first). Other XHS options: general | time_descending |
# comment_count_descending | collect_count_descending.
SOCIALDATAX_TREND_SCOUT_SORT = "like_count_descending"

# Per-API-call price in USD for run cost accounting. 0.0 = don't track.
# Set to your SocialDataX plan's effective per-call cost.
SOCIALDATAX_COST_PER_CALL_USD = 0.0

# PRE runs on nearly every strategy build (1 call, high value) → default on.
# POST fans out per direction (more calls) → opt-in.
ENABLE_SOCIALDATAX_TREND_SCOUT_PRE = True
ENABLE_SOCIALDATAX_TREND_SCOUT_POST = False

# Failure policy for PRE. True (default) = REQUIRED / fail-fast: when PRE
# is enabled and the scout can't deliver calibration posts (missing key,
# call failure, zero results), the run STOPS right there with a clear
# error. Rationale: A1 runs before secretariat, so failing here costs only
# the 太子 stage — whereas silently proceeding without calibration data
# skews the whole strategy and forces a full (expensive) rerun. Two
# exceptions never block: (1) the target platform isn't supported by
# SocialDataX at all, and (2) a revision/resume where the brief already
# carries usable _trend_intel from a previous attempt (reused instead).
# Set False to restore pure advisory behavior (skip + proceed).
# POST is ALWAYS advisory — it runs after all content is produced, so a
# failure there only loses the side-by-side references, never the output.
SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED = True

# How many posts to keep per invocation (ranked by real engagement).
SOCIALDATAX_TREND_SCOUT_TARGET_COUNT = 10

# ── 参考帖抓取(用户在第 2 页粘贴的帖子 URL)────────────────────────────
# v0.32.0: 这个岗位原本靠 Gemini 的 url_context 工具去抓用户贴的小红书链接。
# Kimi 没有等价的"模型自己去取 URL"能力,所以改走 SocialDataX:从 URL 里
# 解析出 note_id,调 detail 工具拿第一方结构化正文,再交给 Kimi 做分析。
# 副作用是比 Gemini 那条路更好 —— url_context 面对 JS 渲染 / 登录墙的小红书
# 经常只拿到 og:title,SocialDataX 拿的是真正文 + 真互动量。
#
# 工具名已按实际调用返回体里的 `tool` 字段核实(XHS 直接验过
# xhs_get_note_detail_by_note_id;其余平台同一套命名规律)。
#
# 每个平台都有 by-ID 和 by-URL 两个变体。**默认走 by-URL**:
#   - 我们手上本来就是用户粘贴的完整 URL,不需要先解析 ID
#   - 小红书链接里的 xsec_token 是访问凭证,留在 URL 里整条传过去最稳,
#     手工拆出来再拼回参数只是给自己找 bug
# by-ID 是兜底:by-URL 调不通(短链、URL 变形)时,从 path 里抠出 ID 再试一次。
#
# 值的形状:(工具名, 参数名)。参数名跟工具名 `_by_` 后面那截一致 ——
# 这是 SocialDataX 自己的命名规律,不是巧合,但仍然显式写出来,免得哪天
# 对不上时只能靠猜。
#
# MCP 工具名和 CLI 是同一套接口:
#   npx -y socialdatax-skills@latest xhs detail --url <url>
#   npx -y socialdatax-skills@latest xhs detail --note-id <id>
# CLI 内部就是转发到下面这些工具。
#
# 名字万一对不上也不会炸:该 stage 是 advisory-only,工具不存在时 MCP 报错
# → 该条 URL 记 not_accessible,其余照常,流水线不受影响。
SOCIALDATAX_NOTE_DETAIL_TOOLS: dict[str, dict[str, tuple[str, str]]] = {
    "xhs": {
        "by_url": ("xhs_get_note_detail_by_note_url", "note_url"),
        "by_id": ("xhs_get_note_detail_by_note_id", "note_id"),
    },
    "douyin": {
        "by_url": ("douyin_get_video_detail_by_url", "url"),
        "by_id": ("douyin_get_video_detail_by_aweme_id", "aweme_id"),
    },
    "kuaishou": {
        "by_url": ("kuaishou_get_video_detail_by_url", "url"),
        "by_id": ("kuaishou_get_video_detail_by_photo_id", "photo_id"),
    },
    "weibo": {
        "by_url": ("weibo_get_post_detail_by_post_url", "post_url"),
        "by_id": ("weibo_get_post_detail_by_post_id", "post_id"),
    },
    "wechat": {
        "by_url": ("wechat_get_video_detail_by_url", "url"),
        "by_id": (
            "wechat_get_video_detail_by_encrypted_object_id",
            "encrypted_object_id",
        ),
    },
}

# 单次参考帖分析最多抓几条,防止用户粘 50 个链接把 run 拖死 + 烧配额。
REFERENCE_ANALYZER_MAX_URLS = 10

# Max output tokens per assist call. 16K handles 6-cell structure reviews
# without truncation. K2.6 supports far more, but our critic/reviewer/vision
# outputs are typically 2-8K so the model stops early anyway.
KIMI_ASSIST_MAX_OUTPUT_TOKENS = 16384

# 兼容别名(v0.31 名字)。
GEMINI_MAX_OUTPUT_TOKENS = KIMI_ASSIST_MAX_OUTPUT_TOKENS


# ── Prompt Caching ────────────────────────────────────────────────────────
# When True, the system prompt is sent with cache_control={"type":"ephemeral"}
# so Anthropic caches it for ~5 minutes. Re-use across retries/batches hits
# the cache and pays ~10% input-token rate for the system portion. Requires
# system prompt to be ≥ 1024 tokens (small prompts silently don't cache).
#
# Vertex AI: native support.
# Anthropic direct: native support.
# Relay proxies: depends on the relay. Most pass the field through; some
# drop it (cache just misses, no error). If your relay 400s on it, set this
# to False.
ENABLE_PROMPT_CACHING = True

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────

# v0.32.0: 当前在用的三款按厂商官网 2026-08 公开价填。走中转的话实际单价
# 以中转账单为准;这张表只影响成本展示,不影响 token 熔断。
#
# 官网价 (USD / 1M token, cache-miss input):
#   kimi-k3            $3.00 in · $15.00 out   (cache hit $0.30)
#   kimi-k2.6          $0.95 in · $4.00  out   (cache hit $0.16)
#   deepseek-v4-flash  $0.14 in · $0.28  out   (cache hit $0.0028)
#
# ⚠️ 缓存命中价没进这张表 —— _estimate_call_cost_usd 对 cache_read token
# 另有折算逻辑(见 agents/__init__.py)。三款模型的 cache 折扣都很猛
# (K2.6 省 83%,v4-flash 省 98%),所以 ENABLE_PROMPT_CACHING 别关。
COST_PER_1M_INPUT: dict[str, float] = {
    "kimi-k3": 3.00,
    "kimi-k2.6": 0.95,
    "deepseek-v4-flash": 0.14,
    # ── 以下已无 stage 指向,仅供历史 run 的成本回显 ──────────────────
    # DB 里 v0.31 及之前的 stage_log 存着这些模型名,删掉的话老 run 的
    # total_cost_usd 会回显成 $0。保留不占运行时成本。
    "deepseek-v4-pro": 0.5,   # 估算值(v0.30.8~v0.31 的 persona_simulator_alt)
    "gpt-5.5": 2.5,           # 估算值(v0.30.7~v0.31 的门下省/兵部)
    "claude-opus-4-8": 15.0,
    "claude-opus-4-7": 15.0,
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "kimi-k3": 15.00,
    "kimi-k2.6": 4.00,
    "deepseek-v4-flash": 0.28,
    # ── 历史 run 回显用,同上 ────────────────────────────────────────
    "deepseek-v4-pro": 2.0,
    "gpt-5.5": 10.0,
    "claude-opus-4-8": 75.0,
    "claude-opus-4-7": 75.0,
    "claude-opus-4-6": 75.0,
    "claude-sonnet-4-6": 15.0,
}

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_PLATFORM = "小红书"

# ── Vibe loop parameters ──────────────────────────────────────────────────
VIBE_LOOP_HARD_CAP = 3       # absolute max iterations
VIBE_LOOP_INITIAL_CAP = 2    # start with this many rounds
VIBE_LOOP_ESCALATE_THRESHOLD = 0.30  # failure rate to unlock extra round

# v0.29.0: critic-rewriter 责任分流 feature flag
# 打开时:按 critic 输出的 root_cause_kind 把 fail 的 cell 分流到
#   - structural_rewriter(身份错位 / 缺口方向错)
#   - vibe_rewriter(表层钩子弱 / 模板性 fail)
#   - strategic_warnings(策略层错配,rewriter 改不了)
# 关闭时:全部塞给 vibe_rewriter(v0.28 及之前的行为),作为稳妥兜底。
ENABLE_STRUCTURAL_REWRITER = True

# v0.29.1: 策略层自动升级(C.2.1)— vibe_loop 结束后若仍有 strategic_warnings,
# 自动回 secretariat 修订受影响的 direction(更新 stop_trigger / reward_type /
# role_embodiment 等锚点),然后再跑一次 vibe_loop 让 critic + rewriter
# 用新锚点重新判决。关闭时保持 v0.29.0 行为(只写 warnings 由用户人工介入)。
ENABLE_STRATEGIC_ESCALATION = True
# 硬上限:策略层循环的最大轮数。默认 1 — 只允许一次自动升级,避免 direction
# 来回摆动陷入死循环。超过后 strategic_warnings 仍然会写到 final_system
# 让用户人工处理。
STRATEGIC_LOOP_MAX_ITERATIONS = 1

# v0.29.1: 消费者模拟(C.2.2)— 在 vibe_loop 结束后、终审前,让
# persona_simulator 以 stop_trigger 描述的具体目标用户身份对每个 cell
# 做 stop / scroll 二元判决,作为 interest_align 的第二层校验。结果存到
# final_system._consumer_simulation,UI 显示;cell 若被目标用户 scroll,会
# 追加进 strategic_warnings 走人工审查通道。
ENABLE_CONSUMER_SIMULATION = True

# v0.33.2: 消费者模拟只跑【critic 判 interest_align=weak】的 cell,不再全量跑。
#
# 病灶:到消费者模拟这一步,同一批 demo 已经被画像类 agent 判过两遍
# (persona_simulator 主 + alt 双 backend),而消费者模拟调的**就是同一个
# persona_simulator**,只是 mode 换成 consumer_simulation、判据从
# target_audience 换成 stop_trigger —— 同一个 agent 对同一批内容判第三遍。
#
# 更关键的是它的定位:"interest_align 的第二层校验"。但 vibe_critic 的四乘数
# 硬门槛**已经逐 cell 判过 interest_align 了**。对 critic 判 pass 的 cell 再
# 全量复判一遍,拿到的多半是同一个答案;对 critic 判 fail 的 cell,它们要么
# 已经进了 rewriter 要么已经进了 strategic_warnings,也不需要这一票。
#
# 真正有价值的是 **weak** 那一档 —— critic 自己都拿不准的。把二次意见花在
# 这里,才是"第二层校验"该有的样子。
#
# 数据来源是 P1 建的 orchestrator._vibe_cell_reviews(vibe_loop 跨轮次累积的
# cell_reviews),零额外成本。拿不到 review 时(critic 挂了 / resume 路径)
# 退回全量跑 —— 那种情况下恰恰最需要第二层校验。
CONSUMER_SIM_ONLY_WEAK_ALIGN = True

# v0.33.2: 画像模拟的"弱 cell"判据从【任一 backend 全 skip】改成
# 【两个 backend 都全 skip】。
#
# 病灶是**成本不对称**,不是准确率。原来的并集规则方向很明确:多加一个
# backend 主要提高的是误报率(6 个画像里任意一组 3 票全 skip 就判弱),
# 而误报的代价极不对称 ——
#   弱 cell → strategic_warnings → 触发 ENABLE_STRATEGIC_ESCALATION
#          → 回中书省(kimi-k3)改方向 → **再跑一整轮 vibe_loop**
# 一次几分钱的 DeepSeek 调用,能触发全流水线最贵的一次重入。
#
# 交集规则要求两个不同厂家、不同 distribution 的画像**都**一致否决才算弱。
# 这才是当初双 backend 的本意:跨厂家一致 = 强信号;单边否决 = 噪声。
# 设 False 回到 v0.30.8~v0.33.1 的并集行为。
PERSONA_WEAK_REQUIRES_BOTH_BACKENDS = True

# v0.33.2: 叙事导演的 cell 重建上限。
#
# 叙事导演诊断出跨 cell 问题后会**逐个重跑 works_builder** 来重建 cell ——
# 而 works_builder 是全流水线 token 消耗第一的 stage(15.8K 字符系统提示词 +
# 完整 cell_plan + brief + 4500 字符输出)。诊断本身很便宜(2101 字符提示词、
# 只看 demo 前 500 字),贵的全在重建。
#
# 它管的"钩子重复 / 跨 cell 撞车"还有另外两处覆盖:
#   - `_find_cross_cell_duplicates` —— 确定性、零成本,而且跑两遍
#     (builder 结束一次 + vibe 之后一次)
#   - `vibe_critic.md` 第 3 步「跨 cell 一致性检查(batch 内必做)」
# 三重覆盖里只有叙事导演这一路会触发最贵的重建。
#
# 设上限而不是直接关掉:它的"正反面叙事比例失衡"这类判断确实是另外两处
# 看不到的。0 = 只诊断不重建(纯 advisory);None = 不限(v0.33.1 及之前的行为)。
NARRATIVE_DIRECTOR_MAX_REBUILDS = 3

# ── 批量采样验收 (v0.33.3) ───────────────────────────────────────────────
# 流水线交付的是「批量生成用的」system_prompt(要产出 N≥10 篇),但在此之前
# 每一道质量闸看的都是每个 cell 唯一那篇 demo_output —— 11 道闸,1 个样本。
#
# 后果:决定"第 7 篇会不会和第 3 篇一个味"的 5 池 + 人设轮换机制从来没被验证
# 过(唯一的检查是数一数池子的文字在不在 prompt 里,**存在 ≠ 有效**);而且
# 那篇 demo 被红蓝 + 网感重写打磨过最多 3 轮,测的是"这 8 道工序能修到多好"
# 不是"这段 prompt 平均能生成什么"。
#
# 这一层拿建好的 system_prompt 真跑 N 篇,**只变 {{seed}}** —— 那正是
# works_builder.md 批量生成规则承诺的差异化开关。变了 seed 还是一个味,
# 就说明 5 池轮转是写在纸上的。
#
# ⚠️ 当前是【观测,不拦】:采样结果不阻塞出货、不触发重写、不影响 verdict。
# 因为本仓库还没有历史分布数据,没有基线就设阈值等于凭猜调参 —— 那正是这轮
# 改造要治的病。先攒几条 run 的真实分布,再决定阈值定在哪、要不要升级成闸门。
#
# 成本:走辅助层(不占 MAX_TOKENS_PER_RUN),system_prompt 命中 prompt cache
# (K2.6 省 83%),每次只出一篇。12 格子 × 5 篇 ≈ 60 次 ≈ $0.15-0.3/run。
ENABLE_BATCH_SAMPLING = True

# 每个 cell 采几篇。5 是起步值:够看出 5 池轮转是否生效,对"20% 废稿率"这种
# 问题有约 67% 的检出概率(1 - 0.8^5)。跑通并攒出分布后再按实测调。
# 抬到 8 检出率约 83%;抬到 20 才够统计上区分 15% 和 25% 的失败率,但对每条
# run 都跑那个量偏重 —— 那更适合专门的调参场景而非常规出货。
BATCH_SAMPLE_N = 5

# 相邻两篇的 seed 间隔。对齐 works_builder.md 批量生成规则第 4 条
# 「相邻两篇的 seed 值建议间隔 ≥ 20」—— 用它自己承诺的口径去测它。
BATCH_SAMPLE_SEED_STEP = 20

# 并发上限。采样走辅助层、不受主链路限流器管,所以这里自己收着点,
# 别把 Moonshot 的配额挤给主链路。
BATCH_SAMPLE_CONCURRENCY = 5

# 单条 run 最多采样几个 cell。格子数 = 方向数 × 平台数,被策略升级刷上去时
# 采样量是 cells × N 的乘积。超出部分**显式报告**不静默丢弃(见
# batch_sampler.run_batch_sampling)—— 无声截断会被读成"全采了"。
BATCH_SAMPLE_MAX_CELLS = 20

# 单次采样的输出上限。一篇小红书 300-800 字、知乎最长 1500 字,2048 token
# 足够;给太大只会让模型倾向写更长,反而偏离平台真实长度。
BATCH_SAMPLE_MAX_OUTPUT_TOKENS = 2048

# ── 跨批次多样性:开头切入角度编号库 (v0.33.4) ───────────────────────────
#
# 病灶:交付的 system_prompt 里有 {{seed}} 做批内差异化,但**没有任何跨批次
# 机制**。运营连着跑 5-10 个批次之后必然撞车,而且撞了没人看得出来 ——
# LLM 是无状态函数,它不知道昨天用同一个 prompt 生成过什么。
#
# 修法不是新加一套机制,是**给现有的「开头切入池」扩容并编号**。
# 5 池里的 opening_angle 原来只要求"至少 5 个",5 种粒度跑 5-10 批就把组合
# 用光了;拆到 15 种编号后能跑 20-30 批才耗尽。这是纯粒度问题,不需要新模块 ——
# 而 works_builder.md 的字数预算刚在 v0.33.1 收紧过,硬塞新机制会把它再撑爆。
#
# 编号(C01-C15)而不是纯文字描述,是为了让「历史回避清单」能落地:运营粘贴
# 「上批用过 C03/C07/C11」比粘贴一堆角度描述可操作得多。
#
# ⚠️ 诚实边界:这是**补偿**不是根治。跨批次去重的根本解法是工作台维护一个
# 已生产资产库、每次生成自动注入避重池 —— 提示词层做不到这件事。产出中心
# 会把这句话如实写给运营看,不要让人以为这是终极方案。
CONTENT_ANGLE_LIBRARY: tuple[tuple[str, str], ...] = (
    ("C01", "物证发现型"),   # 翻出他口袋里的外卖小票
    ("C02", "数字刺激型"),   # 体检报告上那个 6.1
    ("C03", "对话切片型"),   # 我说 X / 他说 Y / 我说 Z
    ("C04", "单方独白型"),   # 凌晨三点我在想
    ("C05", "反常记录型"),   # 他今天做了件不一样的事
    ("C06", "信息差揭露型"), # 其实这个成分是…
    ("C07", "反直觉科学型"), # 你以为 A 导致 B,其实…
    ("C08", "行业秘史型"),   # 这个认证十几年前被…
    ("C09", "权威身份断言"), # 做这行六年的我告诉你
    ("C10", "诚实退让型"),   # 我不否认 X,但…
    ("C11", "代际对比型"),   # 80 年代的 4.8 vs 现在的 5.9
    ("C12", "个案观察型"),   # 我观察了我爸三个月
    ("C13", "群体处境型"),   # 这个处境的人都懂
    ("C14", "静态定格型"),   # 那个下午没人说话
    ("C15", "自我修正型"),   # 我之前以为 X,最近发现…
)

# 单批次要求覆盖的最少编号数。批量按 10 篇算,覆盖 ≥8 种意味着最多两篇同编号。
CONTENT_ANGLE_MIN_COVERAGE = 8


def format_angle_library() -> str:
    """把编号库拍成一行紧凑文本,供提示词嵌入。

    刻意做成一行:works_builder 的 system_prompt 有字符预算(见 works_builder.md
    规则 10 的预算表),15 条角度写成多行列表会占掉 300+ 字符,压成一行 ≈150。
    """
    return " / ".join(f"{code} {name}" for code, name in CONTENT_ANGLE_LIBRARY)


# ── Advisory stage concurrency ────────────────────────────────────────────
RED_BLUE_CONCURRENCY = 5
TREND_SCOUT_POST_CONCURRENCY = 5

# ── UI polling ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 3

# ── Stage ordering (for display) ──────────────────────────────────────────

# The third tuple element (stage index/marker) is intentionally a short
# numeric string rather than an emoji — the UI renders it as a flat,
# typographic step marker. Kept as a 3-tuple so existing unpackers
# `(key, label, marker)` stay valid.
PIPELINE_STAGES = [
    ("crown_prince", "太子", "01"),
    # Advisory-only (Gemini). Skipped if user didn't paste URLs on
    # page 2 OR if Gemini isn't configured. Fetches user-specified
    # xiaohongshu post URLs via url_context — higher-signal than
    # keyword search because the user directly picked the references.
    ("gemini_reference_analyzer", "参考帖子·SocialDataX", "02"),
    # SocialDataX first-party trend sampling. Pulls real current 小红书
    # 爆款 (原文 + 互动量, engagement-ranked) and injects them into
    # brief._trend_intel so secretariat's strategy is calibrated against
    # concrete current examples, not abstract guesses. REQUIRED by
    # default (SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED) — fail-fast beats
    # silently producing an uncalibrated run. Stage key keeps the
    # historical "gemini_" prefix for stage-log/UI compatibility.
    ("gemini_trend_scout_pre", "趋势取样·SocialDataX", "03"),
    ("secretariat", "中书省", "04"),
    ("chancellery", "门下省", "05"),
    ("dispatcher", "尚书省", "06"),
    ("ministry_personnel", "吏部", "07"),
    ("ministry_revenue", "户部", "08"),
    ("ministry_rites", "礼部", "09"),
    ("ministry_war", "兵部", "10"),
    ("ministry_justice", "刑部", "11"),
    ("ministry_works", "工部·架构", "12"),
    ("ministry_works_cell_planner", "工部·格子规划", "13"),
    ("ministry_works_builder", "工部·构建", "14"),
    ("narrative_director", "叙事导演", "15"),
    ("red_blue_red", "红队·攻", "16"),
    ("red_blue_blue", "蓝队·守", "17"),
    ("persona_simulator", "画像模拟·Kimi", "18"),
    ("persona_simulator_alt", "画像模拟·DeepSeek", "19"),
    ("ministry_works_structure_review", "结构审·Kimi", "20"),
    ("vibe_critic", "网感复检", "21"),
    # v0.29.3: 补展示 — 这两个阶段其实一直在跑也各自记 stage_log,
    # 但 PIPELINE_STAGES 漏了,导致 Settings 页面"模型配置"看不到它们
    # 用的是哪个模型(用户手动在 secrets 里配了 override 也找不到对应行)。
    ("vibe_rewriter", "网感重写", "22"),
    ("structural_rewriter", "叙事结构重写", "23"),
    ("chancellery_final", "终审", "24"),
]
