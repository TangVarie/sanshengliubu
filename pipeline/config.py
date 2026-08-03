"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.32.0"
VERSION_DATE = "2026-08-03"
VERSION_NOTES = (
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
    "参考帖抓取改走 SocialDataX note-detail (Gemini url_context 无对应物),"
    "google-genai 依赖移除。"
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

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # plan_review: force pass on round 3 (legacy, used by non-debate path)

# ── Strategy Debate ──────────────────────────────────────────────────────
# Max turns in the secretariat ↔ chancellery multi-turn debate.
# Secretariat speaks on even turns, chancellery on odd. So MAX_DEBATE_TURNS=8
# means 4 exchanges (each agent speaks 4 times). Chancellery can approve
# at any odd turn to end early. Last chancellery turn is force-approve.
MAX_DEBATE_TURNS = 8

# final_review (工部产出的 prompt_matrix) 的轮次上限。第一次跑流水线 = round 1；
# 用户每点一次「应用修订意见并重跑」round +1。超过 MAX_FINAL_REJECTIONS 后强制
# 放行，并在 suggestions 里打风险注。防止终审无限驳回工部造成死循环。
MAX_FINAL_REJECTIONS = 3

# ── Token limits ───────────────────────────────────────────────────────────
# max_tokens must accommodate (thinking_budget + actual_output) for thinking stages.

MAX_TOKENS_DEFAULT = 16000
MAX_TOKENS_STRATEGY = 32000  # strategy/review stages need most room

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

# ── Extended Thinking ─────────────────────────────────────────────────────
# 6 strategy/review/compliance stages use extended thinking (budget_tokens
# on relay, adaptive on Vertex/4.6+). Execution stages skip thinking for speed.
#
# v0.30.12: ministry_justice 加入。历史上它靠模型名后缀
# claude-opus-4-6-thinking 拿 extended thinking;当可用模型表收敛到 base
# 模型(无 -thinking 变体)后,后缀路径失效。改为走 THINKING_STAGES +
# adaptive thinking JSON 参数,保证合规审查仍有深推理(安全/法务把关不能
# 悄悄变弱)。

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
# v0.32.0 换厂后同样的 2M token 上限便宜了一个数量级:kimi-k2.6
# ($0.95/M in + $4/M out) 下 2M token ≈ $5/run;即使全跑 kimi-k3
# ($3/$15) 也就 ≈ $18(换厂前 Opus 4.7 是 ≈ $90)。上限保持 2M 不动 ——
# 它的作用是"挡住重试风暴/死循环",不是控成本档位,按 token 计更稳。
MAX_TOKENS_PER_RUN = 2_000_000

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
# ⚠️ 工具名待你按自己的 SocialDataX 套餐核对。socialdatax-skills v0.2.30 的
# CLI 里已验证的只有各平台的 *_search_* 工具(见 socialdatax_trend_scout.py
# 的 _SEARCH_SPEC),detail 类工具没在那份 CLI 里出现过,所以下面这张表是
# 按同一套命名规律推的。名字不对不会炸 —— 该 stage 是 advisory-only,
# 工具不存在时 MCP 报错 → 整个 stage 记 skipped,流水线照常往下走
# (和 v0.31 里 Gemini 抓不到时的行为一致)。
# 核对方法:设置页 →「SocialDataX」区有「列出可用工具」按钮。
SOCIALDATAX_NOTE_DETAIL_TOOLS: dict[str, str] = {
    "xhs": "xhs_get_note_detail",
    "douyin": "douyin_get_video_detail",
    "kuaishou": "kuaishou_get_video_detail",
    "weibo": "weibo_get_post_detail",
    "wechat": "wechat_get_video_detail",
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
