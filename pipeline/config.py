"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.30.12"
VERSION_DATE = "2026-06-10"
VERSION_NOTES = (
    "v0.30.12 chore: Claude 模型表收敛到 4 款可用模型。Claude 侧只剩 "
    "claude-opus-4-6 / 4-7 / 4-8 + claude-sonnet-4-6;非 Claude backend "
    "(GPT via vectorengine / DeepSeek / Gemini) 保留原配置不动。"
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
# All stages use the same Claude model family. Whether thinking is enabled
# is controlled per-call via the standard Anthropic API
# `thinking={"type":"enabled","budget_tokens":N}` parameter — see
# THINKING_STAGES below + agents/__init__.py::_call_claude.
#
# Why one model name in most presets: Anthropic native, modern relay
# proxies, and Vertex all accept the standard JSON `thinking` parameter.
# The old convention of using a `-thinking` suffix in the model name was
# a relay-specific routing hack. Keeping a single model name also makes
# prompt caching cache across thinking and non-thinking stages (same
# system prompt + same model = same cache key).
#
# ── MODEL_PRESET options ─────────────────────────────────────────────
# Change this string to switch strategy/content model split without
# editing MODELS directly:
#
#   "all_opus"      (default, current behavior) — every stage on Opus.
#                   Deepest reasoning; most expensive; Opus has a slightly
#                   more "精致/端正" voice that some reviewers call AI-toned.
#
#   "content_sonnet" — Strategy + review stages stay on Opus; content-
#                   producing stages (works_builder, vibe_critic,
#                   vibe_rewriter) switch to Sonnet 4.6. Rationale:
#                   Sonnet's demo output tends to read more "松弛/人味",
#                   and critic-style tasks benefit from a lighter tone.
#                   Cheaper + faster on the content-heavy stages.
#                   RECOMMENDED for experimentation if output still feels
#                   AI-toned after vibe rewriter.
#
#   "all_sonnet"   — Everything on Sonnet. Cheapest, fastest, but
#                   reasoning-heavy stages (secretariat, chancellery,
#                   chancellery_final) may produce lower-quality plans.
#                   Mostly useful for dev loops / cost-tight pilots.

OPUS_MODEL = "claude-opus-4-7"
# 用于 all_sonnet preset 以及 planning / 结构化任务(planning 阶段需要
# Sonnet 4-6 的稳定 JSON 输出能力)。
SONNET_MODEL = "claude-sonnet-4-6"
# v0.30.12: Claude content 写作模型。老版本 Sonnet 3.7 已退出可用模型表
# (当前 Claude 只剩 opus 4-6/4-7/4-8 + sonnet 4-6),所以 content 角色从
# Sonnet 3.7 切到 Sonnet 4-6。继续保留独立常量,语义上"内容写作用更轻
# 的模型",但当前指向和 SONNET_MODEL 等价。哪天 Sonnet 4-7 上线,只需要
# 改这一行就能把所有 content stage 切过去。
SONNET_CONTENT_MODEL = "claude-sonnet-4-6"

# v0.30.0: 在代码层面直接锁定每个 stage 用哪个模型(高质量配置),用户
# 不必再在 secrets.toml 里维护 model_overrides。可选 preset:
#
#   premium_multi_vendor(v0.30.0 默认,推荐) — 不限成本求最优组合,
#     - 推理 / 长上下文 / 中文锐度都拉满 (Claude Opus 4-7 担纲)
#     - 中书省 (Claude) vs 门下省 / 兵部 (GPT) 故意用不同厂家避免辩论同色彩
#     - 内容生成走 Sonnet 4-6(v0.30.12 起;原 Sonnet 3.7 已退役)
#     - persona_simulator_alt 用 DeepSeek 提供异厂家画像视角
#     - 需要 secrets.toml 同时配 Claude 中转 + VECTORENGINE_API_KEY(GPT) +
#       (可选)DEEPSEEK_API_KEY
#
#   content_sonnet(v0.29.x 历史默认) — 保留兼容
#   all_opus / all_sonnet — 单档全跑,降级方案
MODEL_PRESET = "premium_multi_vendor"

# 各阶段精确模型映射 — 写在代码里防止 secrets.toml 误覆盖。
# Claude stage 用当前可用的 4 款 (opus 4-6/4-7/4-8 + sonnet 4-6);
# 非 Claude backend (GPT via vectorengine / DeepSeek) 保留原配置不受影响。
# v0.30.12: Claude 侧的 -thinking 后缀和 Sonnet 3.7 全部退役;thinking
# 行为由 agents/__init__.py 里 adaptive thinking JSON 参数控制 (Opus 4.6+
# 自带),不再走模型名后缀路径。
PREMIUM_MULTI_VENDOR_MAP: dict[str, str] = {
    # ── 策略 / 推理核心(深推理,Opus 4.7)──
    # v0.30.2: 不带 -thinking 后缀,tdyun 上 -thinking 没定价。Opus 4.7
    # base 自带内部 reasoning。
    "crown_prince": "claude-opus-4-7",                    # 太子(整理 + 索引)
    "secretariat": "claude-opus-4-7",                     # 中书省(策略发言)
    "chancellery_final": "claude-opus-4-7",               # 终审(holistic 把关)
    "ministry_works": "claude-opus-4-7",                  # 工部架构(整脊柱)
    # ── 异厂家辩论(Claude vs GPT)──
    # 用 GPT 提供和 Claude 不同 distribution 的异色彩对抗,比同厂家
    # 自言自语更能挑出问题。GPT 走 vectorengine OpenAI-compat 后端。
    "chancellery": "gpt-5.5",                             # 门下省(critic)
    "ministry_war": "gpt-5.5",                            # 兵部(刁钻竞争)
    # ── 结构化派发 / 五部 — Opus 4.6 稳态 ──
    "dispatcher": "claude-opus-4-6",
    "ministry_revenue": "claude-opus-4-6",
    "ministry_rites": "claude-opus-4-6",
    "ministry_justice": "claude-opus-4-6",                # 合规要严(v0.30.12: -thinking 后缀去掉,用户表只有 base)
    "ministry_works_cell_planner": "claude-opus-4-6",
    # ── 创意 / 内容相关阶段:判断走 Opus 4.6,纯写作走 Sonnet 4.6 ──
    # 历史上写作用 Sonnet 3.7 (网感强),v0.30.12 起模型表里没了,统一切 4.6。
    "ministry_personnel": "claude-opus-4-6",              # 画像创作(创意)
    "narrative_director": "claude-opus-4-6",              # 跨 cell 一致性诊断(创意判断)
    "vibe_critic": "claude-opus-4-6",                     # 网感复检(judge)
    "structural_rewriter": "claude-opus-4-6",             # 身份/缺口手术(content 重写)
    "ministry_works_builder": "claude-sonnet-4-6",        # 内容写作(v0.30.12: 原 Sonnet 3.7)
    "vibe_rewriter": "claude-sonnet-4-6",                 # 内容重写(v0.30.12: 原 Sonnet 3.7)
    # ── 红蓝精炼真对抗:Red (Opus 4.6) vs Blue (Sonnet 4.6) ──
    # 异 distribution: Opus 找 AI 腔指纹和结构问题,Sonnet 接力最小修复
    "red_blue_refiner": "claude-sonnet-4-6",              # legacy 兼容,实际不用
    "red_blue_red": "claude-opus-4-6",                    # 攻方
    "red_blue_blue": "claude-sonnet-4-6",                 # 守方(v0.30.12: 原 Sonnet 3.7)
    # ── 画像模拟双 backend(v0.30.8):主 Claude + alt DeepSeek 异厂家 ──
    "persona_simulator": "claude-sonnet-4-6",             # 主路径(v0.30.12: 原 Sonnet 3.7)
    "persona_simulator_alt": "deepseek-v4-pro",           # DeepSeek 异厂家
}

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
    "persona_simulator": "content",    # simulates real humans (Claude 系)
    "persona_simulator_alt": "content",  # v0.30.8: DeepSeek 异厂家画像
    "vibe_critic": "content",
    "vibe_rewriter": "content",
    # v0.29.0: 叙事结构重写者 — 和 vibe_rewriter 同角色(内容写作),
    # 走 content 池(Sonnet 3.7 网感)。
    "structural_rewriter": "content",
}


def _resolve_models(preset: str) -> dict[str, str]:
    """Assemble the MODELS dict from role tags + preset. Returning a dict
    keeps consumers (logging, cost accounting, settings UI) unchanged.

    角色 → 模型映射(按 preset):

    content_sonnet(默认):
      - content 角色(builder / vibe_critic / vibe_rewriter / red_blue /
        persona_simulator) → SONNET_CONTENT_MODEL(默认 Sonnet 3.7,写作
        人味最重)
      - 其他所有角色(strategy + planning + cross-cell coherence) →
        OPUS_MODEL(默认 Opus 4.7,深推理)

    all_sonnet:
      - content 角色 → SONNET_CONTENT_MODEL(Sonnet 3.7)
      - 其他角色 → SONNET_MODEL(Sonnet 4.6,稳定 JSON 输出)

    all_opus:
      - 全部 → OPUS_MODEL

    注:planning 角色(尚书省 / 六部 / 格子规划)在 content_sonnet 下用 Opus
    (不是 Sonnet),因为 "Structured planning: Opus preferred for stability"
    ——结构化派发需要稳定的指令理解,降到 Sonnet 会偶尔漏字段。如果要
    planning 走 Sonnet 省钱,改用 all_sonnet preset。
    """
    if preset == "all_sonnet":
        # 全 Sonnet 模式:content 用 3.7 保网感,其他用 4.6 保结构
        return {
            k: (SONNET_CONTENT_MODEL if role == "content" else SONNET_MODEL)
            for k, role in _STAGE_ROLES.items()
        }
    if preset == "content_sonnet":
        return {
            k: (SONNET_CONTENT_MODEL if role == "content" else OPUS_MODEL)
            for k, role in _STAGE_ROLES.items()
        }
    if preset == "premium_multi_vendor":
        # v0.30.0:每个 stage 都从 PREMIUM_MULTI_VENDOR_MAP 直接拿模型名;
        # 没在 map 里的 stage(罕见,通常是新增的 stage 还没补)fallback 到
        # OPUS_MODEL,既保证能跑也提示要补。
        return {
            k: PREMIUM_MULTI_VENDOR_MAP.get(k, OPUS_MODEL)
            for k in _STAGE_ROLES
        }
    # Default / fallback: all_opus
    return {k: OPUS_MODEL for k in _STAGE_ROLES}


MODELS: dict[str, str] = _resolve_models(MODEL_PRESET)

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
MATRIX_BATCH_CONCURRENCY = 3    # parallel builder calls
MATRIX_CELLS_PER_BATCH = 1      # one cell per call (safest for JSON structure)

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 3    # parallel cell-planner calls

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
# Tune these to your backend's published limits. Defaults are calibrated
# for a typical paid relay quota (15 RPM / 16 concurrent — actually
# observed on a real account). Set CLAUDE_RPM_LIMIT to 0 to disable the
# rate cap entirely (e.g. on Vertex with high project quota).
#
# Vertex AI mode bypasses this limiter entirely — Vertex enforces quota
# server-side and returns 429 we'd just retry into. See
# agents/__init__.py::_get_active_limiter.
CLAUDE_RPM_LIMIT = 15
CLAUDE_MAX_CONCURRENT = 16

# ── Per-run budget ───────────────────────────────────────────────────────
# Hard ceiling on combined input + output tokens accumulated within a
# single pipeline run. Once exceeded, the next agent call raises
# RunBudgetExceededError and the orchestrator marks the run as failed.
# Safety net against runaway retry loops — 14 stages × worst-case retries
# × thinking budgets can compound quickly without a cap.
#
# Opus at $15/M input + $75/M output → 1M tokens ≈ $45 ceiling per run
# (assuming roughly balanced in/out). Tune to your cost tolerance.
MAX_TOKENS_PER_RUN = 2_000_000

# ── Gemini auxiliary assist (second-opinion critic + structure reviewer) ──
# Independent of the primary Claude backend. When configured, Gemini:
#   1. Re-evaluates cells Claude's vibe_critic passed (B: 分歧仲裁).
#      If Gemini says fail, the cell is sent back to vibe_rewriter.
#      Use case: catch AI-tone outputs Claude's critic gives face-saving
#      borderline → pass scores to.
#   2. Audits structure completeness of every built prompt_cell (5 pools,
#      persona integration, compliance block, keyword list). Output is
#      appended to _revision_directives as advisory notes.
#
# Failure mode: advisory-only. Gemini call errors log a warning and the
# pipeline proceeds with Claude-only verdicts. Never blocks a run.
#
# Auth: Vertex Express API key (the `?key=${API_KEY}` variant — see
# https://cloud.google.com/vertex-ai/docs/general/vertex-express).
# Secrets.toml field: VERTEX_EXPRESS_API_KEY.
ENABLE_GEMINI_ASSIST = True

# Model identifier. Must be in your Vertex Express account's accessible
# model list — use the "📋 列出可用 Gemini 模型" button on the Settings
# page to see exactly what your key can call.
#
# NOTE: Google uses DOTS for decimal version numbers (gemini-2.5-pro,
# gemini-3.1-pro-preview), not dashes. `gemini-3-1-...` is wrong.
#
# Common picks:
#   - gemini-3.5-flash              (released 2026-05; agentic/multimodal lead, cheaper)
#   - gemini-3.1-pro-preview        (best on dense reasoning + long-context recall)
#   - gemini-3.1-pro-preview-customtools  (same + tool-use features)
#   - gemini-3-pro-preview          (earlier Gemini 3 Pro preview)
#   - gemini-2.5-pro                (stable, widely available)
#   - gemini-2.5-flash              (cheap legacy)
#
# This is the GLOBAL DEFAULT — applies to any role not listed in
# GEMINI_MODEL_OVERRIDES below.
GEMINI_MODEL = "gemini-3.1-pro-preview"

# Per-role model override. Each Gemini-driven agent looks itself up here
# (via resolve_gemini_model() in gemini_client.py); if absent, falls back
# to GEMINI_MODEL above.
#
# Why per-role:
#   Gemini 3.5 Flash (2026-05) leads 3.1 Pro on agentic / coding / multi-
#   modal benchmarks, AND is ~40% cheaper. But Flash gives up ground on
#   academic reasoning and dense long-context recall. So the "best" model
#   genuinely depends on the role:
#
#   - Vision (image_transcriber, screenshot_analyzer):
#       Flash wins outright on multimodal understanding → Flash.
#   - Structural checklist (structure_reviewer):
#       Agentic-style task, Flash wins on agentic benchmarks → Flash.
#   - Search-grounded scout (trend_scout):
#       Quality dominated by Google Search grounding (billed separately
#       at ~$35/1k queries); model cost is small fraction → Flash for
#       speed + savings.
#   - Long-content URL reading (reference_analyzer):
#       Posts can be long, dense recall matters → keep Pro.
#   - "网感" / "烟火气" critic (critic):
#       Nuanced judgment on AI-tone. Flash may flag more aggressively or
#       miss subtle borderline cases. Jury still out — kept on Pro for
#       now. Flip to Flash to A/B test; the critic's verdict change rate
#       is the signal you're looking for.
#
# To override globally for ALL roles, leave this empty {} and just edit
# GEMINI_MODEL above. To pin one role to a specific model, add it here.
GEMINI_MODEL_OVERRIDES: dict[str, str] = {
    "image_transcriber":   "gemini-3.5-flash",
    "screenshot_analyzer": "gemini-3.5-flash",
    "structure_reviewer":  "gemini-3.5-flash",
    "trend_scout":         "gemini-3.5-flash",
    "reference_analyzer":  "gemini-3.1-pro-preview",
    "critic":              "gemini-3.1-pro-preview",
}

# Trend scout — when True, Gemini runs a live Google Search
# (site:xiaohongshu.com) in two places:
#   - PRE: before secretariat, to enrich the brief with real current
#          post samples (titles + snippets) so strategy is calibrated
#          against concrete examples, not abstract assumptions.
#   - POST: after chancellery_final, per direction, for side-by-side
#           comparison with our produced demos. Advisory, non-blocking.
# Costs: Google Search grounding is billed separately by Google
# (~$35 / 1000 queries). PRE is 1 query/run; POST is 1 query per
# direction (~5-8/run). Budget ~$0.30 extra per full run.
#
# IMPORTANT CONTRACT — scout output is forced to be RAW POSTS ONLY,
# never trend analysis. See pipeline/prompts/gemini_trend_scout.md +
# pipeline/agents/gemini_trend_scout.py _FORBIDDEN_SUMMARY_KEYS.
ENABLE_GEMINI_TREND_SCOUT_PRE = False
ENABLE_GEMINI_TREND_SCOUT_POST = False
# How many posts to ask the scout to pull per invocation. Each post is
# ~150 chars in the output, so 10 is a reasonable default — gives
# secretariat meaningful calibration without ballooning the prompt.
GEMINI_TREND_SCOUT_TARGET_COUNT = 10

# Max output tokens per Gemini call. 16K handles 6-cell structure
# reviews without truncation. Grounding calls auto-bump to 24K (see
# gemini_client.py). Gemini 3.x supports up to 64K output, but 16K
# is a good default ceiling — our critic/reviewer/scout outputs are
# typically 2-8K so the model will stop early anyway.
GEMINI_MAX_OUTPUT_TOKENS = 16384

# Rough per-1M-token prices (USD) for cost accounting. Fallback used by
# _estimate_cost_usd when the running model isn't in GEMINI_PRICE_TABLE
# below. Intentionally on the high side so reported cost errs toward
# "expensive" rather than "surprise bill".
GEMINI_COST_PER_1M_INPUT = 1.25
GEMINI_COST_PER_1M_OUTPUT = 10.0

# Per-model price table. Keys must match the model IDs used in
# GEMINI_MODEL / GEMINI_MODEL_OVERRIDES. Add new entries when you switch
# to a new model — otherwise the fallback rates above kick in and cost
# accounting drifts.
#
# Sources (verify before billing-critical decisions):
#   - gemini-3.5-flash:        $1.50 / $9.00 per 1M (Google I/O 2026)
#   - gemini-3.1-pro-preview:  $2.50 / $15.00 per 1M
#   - gemini-2.5-pro:          $1.25 / $10.00 per 1M
#   - gemini-2.5-flash:        $0.075 / $0.30 per 1M
GEMINI_PRICE_TABLE: dict[str, dict[str, float]] = {
    "gemini-3.5-flash":             {"input": 1.50,  "output": 9.00},
    "gemini-3.1-pro-preview":       {"input": 2.50,  "output": 15.00},
    "gemini-3.1-pro-preview-customtools": {"input": 2.50, "output": 15.00},
    "gemini-3-pro-preview":         {"input": 2.50,  "output": 15.00},
    "gemini-2.5-pro":               {"input": 1.25,  "output": 10.00},
    "gemini-2.5-flash":             {"input": 0.075, "output": 0.30},
}


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

COST_PER_1M_INPUT: dict[str, float] = {
    # v0.30.12: 只列当前可用的 4 款 Claude。Opus 三代同 tier ($15/$75 in/out),
    # Sonnet 4-6 是轻量 tier ($3/$15)。老条目 (opus-4-1 / sonnet-3-7) 删除,
    # 它们在 MODELS 映射里已无引用;如果有历史 run 在 DB 里引用,
    # _estimate_call_cost_usd 会 fallback 到 0 (见 agents/__init__.py)。
    "claude-opus-4-8": 15.0,
    "claude-opus-4-7": 15.0,
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
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
RED_BLUE_CONCURRENCY = 3
TREND_SCOUT_POST_CONCURRENCY = 3

# ── UI polling ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 3

# ── Stage ordering (for display) ──────────────────────────────────────────

PIPELINE_STAGES = [
    ("crown_prince", "太子", "📋"),
    # Advisory-only (Gemini). Skipped if user didn't paste URLs on
    # page 2 OR if Gemini isn't configured. Fetches user-specified
    # xiaohongshu post URLs via url_context — higher-signal than
    # keyword search because the user directly picked the references.
    ("gemini_reference_analyzer", "参考帖子·Gemini", "🔗"),
    # Advisory-only (Gemini). Skipped if Gemini isn't configured.
    # Pulls real current Xiaohongshu post samples via Google Search
    # (site:xiaohongshu.com), injects raw titles + snippets into
    # brief._trend_intel so secretariat's strategy is calibrated
    # against concrete current examples, not abstract guesses.
    ("gemini_trend_scout_pre", "趋势取样·Gemini", "🔭"),
    ("secretariat", "中书省", "📜"),
    ("chancellery", "门下省", "🔍"),
    ("dispatcher", "尚书省", "📋"),
    ("ministry_personnel", "吏部", "👤"),
    ("ministry_revenue", "户部", "🔑"),
    ("ministry_rites", "礼部", "🎭"),
    ("ministry_war", "兵部", "⚔️"),
    ("ministry_justice", "刑部", "⚖️"),
    ("ministry_works", "工部·架构", "🏗️"),
    ("ministry_works_cell_planner", "工部·格子规划", "📐"),
    ("ministry_works_builder", "工部·构建", "🔨"),
    ("narrative_director", "叙事导演", "🎬"),
    ("red_blue_red", "红队·攻", "🔴"),
    ("red_blue_blue", "蓝队·守", "🔵"),
    ("persona_simulator", "画像模拟·Claude", "👥"),
    ("persona_simulator_alt", "画像模拟·DeepSeek", "🔮"),
    ("ministry_works_structure_review", "结构审·Gemini", "🔎"),
    ("vibe_critic", "网感复检", "🎯"),
    # v0.29.3: 补展示 — 这两个阶段其实一直在跑也各自记 stage_log,
    # 但 PIPELINE_STAGES 漏了,导致 Settings 页面"模型配置"看不到它们
    # 用的是哪个模型(用户手动在 secrets 里配了 override 也找不到对应行)。
    ("vibe_rewriter", "网感重写", "✏️"),
    ("structural_rewriter", "叙事结构重写", "🧱"),
    ("chancellery_final", "终审", "✅"),
]
