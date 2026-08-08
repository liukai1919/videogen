# 视觉导演契约 v1 — 评审稿

状态：提案。本文件不授权任何运行时接入。

本契约定义有证据支撑的脚本、本地顾问型视觉导演、确定性策略裁决和媒体适配器之间的边界。首个实现目标是严格的 JSON/Pydantic 契约，但这份评审稿刻意先确定语义，再编写代码。

## 1. 边界与权力

```text
Script Beat + narration timeline + research context
                         │
                         ▼
                  Director Brief
                         │
                         ▼
            Visual Director (proposal only)
                         │
                         ▼
                  Visual Proposal
                         │
             ┌───────────┴───────────┐
             │ Adjudication Context  │
             │ rights/evidence/time  │
             │ capability/budget     │
             └───────────┬───────────┘
                         ▼
                Visual Adjudication
                    │            │
             approved       policy review
                    │            │
                    └──────┬─────┘
                           ▼
                 Approved Visual Plan
                           │
                           ▼
                      Media Task
                           │
                           ▼
                 composition + release review
```

各参与方的权力刻意保持不对称：

| 参与方 | 可以提案 | 可以驳回 | 可以执行 | 可以发布 |
|---|---:|---:|---:|---:|
| 视觉导演 | 是 | 否 | 否 | 否 |
| 规则引擎 | 否 | 是 | 否 | 否 |
| 媒体适配器 | 否 | 否 | 仅执行已批准计划 | 否 |
| 人工审核者 | 是 | 是 | 可以批准待审核候选 | 是 |

以下边界是强制约定：

- `VisualProposal` 永远不可直接执行，即使它的首选候选看起来安全。
- 权利和证据状态只来自可信应用状态，绝不采信视觉导演输出中的自我声明。
- 渲染器只接收 `ApprovedVisualPlan` 或面向适配器的 `MediaTask`，不接收模型原始输出。
- 人工审核者不能在本流程内覆盖 `reject_candidate`。必须先修正可信状态中的权利或证据，再重新裁决。
- 发布审核始终必需；下文的 `approved` 只表示获准执行，不表示获准发布。

## 2. 标准分类

### 2.1 从 Script Beat 继承的叙事字段

这些词表由上游 Script Beat 模型拥有。契约 v1 引用共享枚举，不另建一份容易漂移的副本。目前已知值包括：

`role`:

- `hook`
- `claim`
- `explanation`
- `counterpoint`
- `transition`
- `conclusion`

`fact_type`:

- `factual`
- `quotation`
- `opinion`
- `transition`

它们都是叙事输入。任何一个字段都不足以独立决定媒介；策略和候选必须把它们与证据、时间一起解释。

### 2.2 媒介和视觉形式是两个独立维度

`media_type` 表示生产路径：

- `source_video`
- `generated_image`
- `generated_video`
- `programmatic_visual`
- `evidence_card`

`visual_form` 表示观众看到的表达形式：

- `source_excerpt`
- `illustrative_scene`
- `concept_metaphor`
- `comparison`
- `diagram`
- `data_chart`
- `timeline`
- `network_topology`
- `evidence_summary`

因此，`comparison` 不是媒介类型。它今天可以由生成图片完成，未来可以换成程序化双栏图，而叙事意义无需改变。这两个维度并非任意组合；每种生产路径仍有明确的合法视觉形式。

`evidence_card` 保留为独立生产路径，因为它由带证据校验的专用模板渲染器生成，而不是通用程序化图解。若未来两者共用同一执行器，可以在新版契约中合并路径，不改变 `evidence_summary` 的视觉语义。

### 2.3 认识论分类

`epistemic_status` 是可信研究元数据：

- `established`
- `supported`
- `disputed`
- `speculative`

`depiction_mode` 是创意表达方式：

- `literal`
- `schematic`
- `metaphorical`

提案可以选择表达方式，但不能把输入的认识论状态提升得更确定。

### 2.4 证据呈现强度

`evidence_presentation` 是每个候选自己的表现选择：

- `none`：不额外显示证据 UI；
- `corner_badge`：低干扰来源角标；
- `source_lower_third`：来源视频上的人物或来源条；
- `full_card`：全屏证据卡；
- `uncertainty_card`：全屏争议或不确定性卡。

证据呈现由可信的 `evidence_display_reasons` 触发，而不是由导演自行宣称风险。原因枚举包括：

- `key_number`
- `direct_quotation`
- `disputed_conclusion`
- `high_risk_claim`
- `source_attribution`
- `legacy_factual_terminal`（仅兼容 profile 可生成）

在 `director_ranked_v1` 中，`full_card` 和 `uncertainty_card` 只用于关键数字、直接引文、争议结论或高风险 Claim；普通事实优先使用角标或不额外打断画面。`legacy_rules_v1` 可以用 `legacy_factual_terminal` 精确复现当前末镜头证据卡。片尾完整引文列表由全片合成层根据所有 Approved Visual Plan 的 Evidence Binding 汇总，不属于单个 Shot 候选。

合法性矩阵：

| 呈现方式 | 可信前提 |
|---|---|
| `none` | 无额外前提 |
| `corner_badge` | 至少一个 Evidence Binding，且主 Claim 至少有一个 `evidence_display_reasons` 项 |
| `source_lower_third` | 非空 Source Binding，且主 Claim 含 `source_attribution` |
| `full_card` | `key_number`、`direct_quotation`、`high_risk_claim` 或仅兼容模式可用的 `legacy_factual_terminal` |
| `uncertainty_card` | `disputed_conclusion` 或 `high_risk_claim`，且认识论状态是 `disputed` 或 `speculative` |

`full_card` 和 `uncertainty_card` 反向要求 `media_type: evidence_card` 且 `visual_form: evidence_summary`；其他生产路径不能模拟全屏证据卡。不满足矩阵或反向媒介约束时必须 `reject_candidate`，不能只给警告。

## 3. 契约对象

所有 ID 都是不透明且稳定的字符串。每个顶层对象都带有 `schema_version: 1`。引入可执行 Schema 后将拒绝未知字段。

版本彼此独立：

- `schema_version` 只描述 JSON 形状和字段语义；
- `policy_version` 描述权利、证据、时间、预算与审核规则；
- `routing_profile` 描述候选排序模式；
- Prompt 使用独立的 `prompt_version`。

修改策略阈值、迁移建议或全片风格目标，不会自动升级 `schema_version`。

为便于本轮一次性评审，本文把对象契约、基线策略和迁移说明放在一起；实现时它们分别落为生成的 JSON Schema、版本化策略配置和迁移文档，互不借用版本号。

共享值对象 `TimeRangeMs` 定义为：

```json
{
  "start_ms": 43800,
  "end_ms": 48100
}
```

它始终表示整数毫秒的左闭右开区间 `[start_ms, end_ms)`，且 `end_ms > start_ms`。所有 Shot、Claim 来源区域、建议来源窗口和已解析来源窗口都复用这个值对象，不使用一端为空的半截区间。

### 3.1 `DirectorBrief` — 给模型的可信输入

```json
{
  "schema_version": 1,
  "brief_id": "brief_neuro_012_02",
  "shot": {
    "shot_id": "shot_neuro_012_02",
    "beat_id": "beat_neuro_012",
    "sequence_index": 17,
    "beat_shot_index": 2,
    "beat_shot_count": 3,
    "time_range": {
      "start_ms": 43800,
      "end_ms": 48100
    },
    "alignment": "word_boundary",
    "narration": "双曲空间可以更紧凑地表示向外快速扩张的连接。"
  },
  "beat": {
    "role": "explanation",
    "fact_type": "factual",
    "claim_ids": ["hyperbolic_space_modeling"],
    "evidence_ids": ["ev_hyperbolic_review_01"],
    "original_contribution": "用空间容量解释这种模型为何适合层级网络。"
  },
  "claims": [
    {
      "claim_id": "hyperbolic_space_modeling",
      "summary": "双曲几何常被用于表示具有层级或快速扩张结构的网络。",
      "epistemic_status": "supported",
      "review_risk": "medium",
      "evidence_display_reasons": ["source_attribution"],
      "evidence_ids": ["ev_hyperbolic_review_01"],
      "source_refs": [
        {
          "source_id": "interview_01",
          "window": {
            "start_ms": 912000,
            "end_ms": 925000
          }
        }
      ]
    }
  ],
  "evidence": [
    {
      "evidence_id": "ev_hyperbolic_review_01",
      "summary": "A review discusses hyperbolic embeddings for hierarchical networks.",
      "datum_keys": []
    }
  ],
  "recent_visuals": [
    {
      "shot_id": "shot_neuro_012_01",
      "media_type": "source_video",
      "visual_form": "source_excerpt",
      "subject": "访谈嘉宾正面中景",
      "composition": "centered_medium",
      "motion": "native",
      "continuity_key": null
    }
  ],
  "next_narrative_role": "counterpoint",
  "production_capabilities": [
    "source_video",
    "generated_image",
    "generated_video",
    "programmatic_visual",
    "evidence_card"
  ]
}
```

强制行为：

- `shot.time_range` 必须是有效的 `TimeRangeMs`。
- `beat_shot_index` 从 1 开始，且不得大于 `beat_shot_count`；二者共同支持首镜头、末镜头和单镜头例外的确定性判断。
- `alignment` 只能是 `word_boundary` 或 `coarse_span`。粗跨度记录真实的 TTS 限制；模型不能在其中虚构切点。
- Beat 引用的每个 Claim 和 Evidence 都必须存在。如果构造 Brief 时发现缺失，系统应向上游报错，不调用导演。
- `source_refs` 只暴露语义来源位置，不代表使用许可。用户授权和权利状态都不发送给视觉导演。
- 条件允许时，`recent_visuals` 包含前面三到五个 Shot。它只是描述性历史，不是硬策略结果。
- 精确数值通过 `datum_keys` 寻址，不以自由文本形式复制进提案。
- `production_capabilities` 只是规划提示。后续 Adjudication Context 才是权威；提案生成后，能力可用性或预算可能已经变化。
- `review_risk` 是 `low`、`medium` 或 `high`；`evidence_display_reasons` 由研究/策略层生成，视觉导演只能选择如何表达，不能删除或降级这些原因。

### 3.2 `VisualProposal` — 不可信的顾问对象

```json
{
  "schema_version": 1,
  "proposal_id": "vp_neuro_012_02_a",
  "brief_id": "brief_neuro_012_02",
  "shot_id": "shot_neuro_012_02",
  "purpose": "解释双曲空间为何适合表示快速扩张的神经连接",
  "candidates": [
    {
      "candidate_id": "cand_neuro_012_02_diagram",
      "rank": 1,
      "media_type": "programmatic_visual",
      "visual_form": "network_topology",
      "visual_brief": "节点从欧氏网格过渡到容纳更多分支的径向网络；只表达结构差异，不显示未经证据绑定的数值。",
      "reason": "空间结构关系比氛围画面更重要。",
      "motion": "slow_push_in",
      "confidence": 0.84,
      "primary_claim_id": "hyperbolic_space_modeling",
      "supporting_claim_ids": [],
      "evidence_bindings": [
        {
          "evidence_id": "ev_hyperbolic_review_01",
          "datum_key": null,
          "use": "structural_relationship"
        }
      ],
      "evidence_presentation": "corner_badge",
      "epistemic_treatment": {
        "kind": "claim_bound",
        "depiction_mode": "schematic",
        "must_not_imply": [
          "该几何模型就是大脑中的物理空间",
          "该模型已经解释意识的产生"
        ],
        "on_screen_qualifier": null
      },
      "continuity_intent": {
        "subject": "由规则网格转变为向外扩张的神经节点网络",
        "composition": "center_to_radial_expansion",
        "palette": "deep_blue_cyan",
        "shot_scale": "medium_to_wide",
        "transition_intent": "conceptual_reveal",
        "continuity_key": "hyperbolic_network_v1",
        "avoid_repetition_of": ["floating_brain", "generic_neuron_closeup"]
      },
      "source_binding": null,
      "estimated_cost_units": 1
    },
    {
      "candidate_id": "cand_neuro_012_02_flux",
      "rank": 2,
      "media_type": "generated_image",
      "visual_form": "concept_metaphor",
      "visual_brief": "深蓝空间中神经节点由平面网格向外展开，明确主体与景深，无文字、标签、数字或水印。",
      "reason": "图解能力不可用时保留核心空间隐喻。",
      "motion": "slow_push_in",
      "confidence": 0.72,
      "primary_claim_id": "hyperbolic_space_modeling",
      "supporting_claim_ids": [],
      "evidence_bindings": [],
      "evidence_presentation": "none",
      "epistemic_treatment": {
        "kind": "claim_bound",
        "depiction_mode": "metaphorical",
        "must_not_imply": [
          "该几何模型就是大脑中的物理空间",
          "该模型已经解释意识的产生"
        ],
        "on_screen_qualifier": null
      },
      "continuity_intent": {
        "subject": "由规则网格转变为向外扩张的神经节点网络",
        "composition": "center_to_radial_expansion",
        "palette": "deep_blue_cyan",
        "shot_scale": "medium_to_wide",
        "transition_intent": "conceptual_reveal",
        "continuity_key": "hyperbolic_network_v1",
        "avoid_repetition_of": ["floating_brain", "generic_neuron_closeup"]
      },
      "source_binding": null,
      "estimated_cost_units": 1
    }
  ],
  "provenance": {
    "provider": "ollama",
    "model": "local-model-name",
    "prompt_version": "visual-director-v1"
  }
}
```

候选的通用字段：

| 字段 | 含义 |
|---|---|
| `candidate_id` | 在提案内唯一，并在审计记录中保持稳定 |
| `rank` | 模型偏好顺序；从 1 开始连续递增 |
| `media_type` | 生产路径，不是叙事功能 |
| `visual_form` | 面向观众的表达形式 |
| `visual_brief` | 语义层创意意图；绝不是最终生成器 Prompt |
| `reason` | 这种媒介为什么适合当前 Shot |
| `motion` | `static`、`slow_push_in`、`slow_pull_out`、`pan_left`、`pan_right` 或 `native` 等运动意图 |
| `confidence` | 当前候选的置信度，范围 `[0, 1]` |
| `primary_claim_id` | 当前 Shot 的主 Claim，类型为 `string | null`；需要表达 Claim 时按镜头相关性显式选择，不能默认取 Beat 的第一个 Claim |
| `supporting_claim_ids` | 当前画面还会表达的辅助 Claim；按相关性排序，可为空 |
| `evidence_bindings` | 该画面所需的来源链引用 |
| `evidence_presentation` | 当前候选采用的证据呈现强度 |
| `epistemic_treatment` | 当前候选自己的确定性表达；不能跨候选共享或从 `visual_form` 猜测 |
| `continuity_intent` | 当前候选与相邻镜头的主体、构图、色板、景别和运动关系 |
| `source_binding` | 来源引用请求；只用于 `source_video` |
| `estimated_cost_units` | 仅供规划的相对成本单位，不是货币；裁决时必须按可信能力数据重算 |

各媒介的附加要求：

| `media_type` | 附加规则 |
|---|---|
| `source_video` | 必须恰好有一个 `source_binding`，包含非空 `source_id`、非空 `source_claim_id` 和可空的 `proposed_window: TimeRangeMs`；`source_claim_id` 必须等于 `primary_claim_id` |
| `generated_image` | `source_binding` 必须为 null；禁止生成文字、标签、数字、Logo 和水印 |
| `generated_video` | `source_binding` 必须为 null；运动必须承担叙事价值；导演不选择具体生成器 |
| `programmatic_visual` | 图解结构必须使用 Evidence Binding；任何显示出来的数字都必须绑定非空 `datum_key`，并具有非空 `primary_claim_id` |
| `evidence_card` | `primary_claim_id` 非空且至少绑定一个 Evidence；`visual_form` 必须是 `evidence_summary`；呈现方式必须是 `full_card` 或 `uncertainty_card` |

提案不变量：

- 一个提案只属于一个 Brief 和一个 Shot。
- 提案包含一到五个候选；ID 唯一，`rank` 必须是无缺口的 `1..n`。
- 列表顺序必须与 `rank` 一致；规则引擎绝不根据置信度自行推断排序。
- 引用的所有 Claim、Evidence、datum 和 source ID 都必须存在于 Director Brief。
- 每个候选的 `confidence` 只表示模型对该媒介适配度的判断，不能授予许可，也不能确立事实。
- 当候选表达事实、引文、来源片段、图解结构或证据卡时，`primary_claim_id` 必须根据 Shot 级语义匹配显式选择；即使 Beat 含多个 Claim，也不得默认取列表第一项。
- 不表达 Claim 的 `opinion` 或 `transition` 候选可以使用 `primary_claim_id: null`，但不得携带 Evidence Binding 或制造可被理解为事实的视觉断言。
- `visual_brief`、`purpose` 和 `reason` 不能引入所引用 Claim 中不存在的事实断言。
- 每个候选都有自己的 `epistemic_treatment` 和 `continuity_intent`；不得把一个候选的表达方式套到其他媒介上。
- `speculative` 或 `disputed` 内容的每个候选至少需要一条 `must_not_imply`。对此类内容进行写实表达必须进入 Policy Review。
- 生成媒体可以解释访谈主题，但不能伪装成档案画面或来源原片。

`epistemic_treatment` 是可区分联合类型：非空 `primary_claim_id` 使用 `kind: claim_bound` 并携带 `depiction_mode`、`must_not_imply` 和 `on_screen_qualifier`；空 `primary_claim_id` 必须只使用 `{ "kind": "not_applicable" }`。

`provenance` 是以 `provider` 为鉴别字段的联合类型：

- `ollama` 必须带有 `model` 和 `prompt_version`，不得带 `builder_version`；
- `rule_baseline` 必须带有 `builder_version`，不得带 `model` 或 `prompt_version`。

规则构造示例：

```json
{
  "provider": "rule_baseline",
  "builder_version": "legacy-proposal-builder-v1"
}
```

无论由谁生成，Visual Proposal 都不能跳过 Visual Adjudication。

### 3.3 来源视频候选示例

视觉导演可以建议语义来源位置，但不能批准使用：

```json
{
  "candidate_id": "cand_neuro_004_01_source",
  "rank": 1,
  "media_type": "source_video",
  "visual_form": "source_excerpt",
  "visual_brief": "使用嘉宾陈述该论断时的正面镜头；优先选择完整手势而非主持人反应。",
  "reason": "让观点回到提出该观点的人。",
  "motion": "native",
  "confidence": 0.78,
  "primary_claim_id": "claim_consciousness_hypothesis",
  "supporting_claim_ids": [],
  "evidence_bindings": [],
  "evidence_presentation": "source_lower_third",
  "epistemic_treatment": {
    "kind": "claim_bound",
    "depiction_mode": "literal",
    "must_not_imply": ["受访者提出的假说已经形成科学共识"],
    "on_screen_qualifier": "受访者观点"
  },
  "continuity_intent": {
    "subject": "提出该观点的访谈嘉宾",
    "composition": "centered_medium",
    "palette": "source_native",
    "shot_scale": "medium",
    "transition_intent": "return_to_speaker",
    "continuity_key": null,
    "avoid_repetition_of": []
  },
  "source_binding": {
    "source_id": "interview_01",
    "source_claim_id": "claim_consciousness_hypothesis",
    "proposed_window": null
  },
  "estimated_cost_units": 0
}
```

`proposed_window: null` 表示“让可信 Source Selector 在 Claim 区域内寻找窗口”，绝不表示“来源的任意部分都能用”。非空时必须是完整的 `TimeRangeMs`。

### 3.4 `AdjudicationContext` — 可信策略快照

```json
{
  "schema_version": 1,
  "context_id": "adjctx_neuro_012_02",
  "brief_id": "brief_neuro_012_02",
  "policy_version": "visual-policy-v1",
  "routing_profile": "director_ranked_v1",
  "source_authorizations": [
    {
      "source_id": "interview_01",
      "user_permission": "quoted",
      "rights_status": "quotation_only"
    }
  ],
  "available_media": [
    "source_video",
    "generated_image",
    "programmatic_visual",
    "evidence_card"
  ],
  "available_programmatic_forms": [
    "data_chart",
    "timeline",
    "evidence_summary"
  ],
  "limits": {
    "normal_min_shot_ms": 2500,
    "normal_max_shot_ms": 6000,
    "ideal_shot_ms": 4000,
    "max_source_excerpt_ms": 6000,
    "generated_video_budget_remaining_units": 0,
    "policy_review_confidence_below": 0.65
  }
}
```

授权必须严格按以下矩阵计算：

| 用户许可 | `owned` | `licensed` | `public_domain` | `quotation_only` | `unknown` |
|---|---:|---:|---:|---:|---:|
| `research_only` | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `quoted` | 允许 | 允许 | 允许 | 允许 | 拒绝 |
| `licensed_media` | 允许 | 允许 | 允许 | 拒绝 | 拒绝 |

任何模型生成字段都不能覆盖这个矩阵。

`routing_profile` 只能是 `legacy_rules_v1` 或 `director_ranked_v1`。前者按现有固定优先级裁决，后者才使用模型候选排名；二者共用同一组版权、证据、时间和预算硬规则。

### 3.5 `VisualAdjudication` — 可审计的规则输出

```json
{
  "schema_version": 1,
  "decision_id": "vadj_neuro_012_02_a",
  "proposal_id": "vp_neuro_012_02_a",
  "context_id": "adjctx_neuro_012_02",
  "shot_id": "shot_neuro_012_02",
  "status": "approved",
  "chosen_candidate_id": "cand_neuro_012_02_flux",
  "fallback_from": ["cand_neuro_012_02_diagram"],
  "candidate_evaluations": [
    {
      "candidate_id": "cand_neuro_012_02_diagram",
      "outcome": "rejected",
      "rule_trace": [
        {
          "rule_id": "PROGRAMMATIC_FORM_AVAILABLE",
          "effect": "reject_candidate",
          "detail": "network_topology is not available in the programmatic renderer"
        }
      ]
    },
    {
      "candidate_id": "cand_neuro_012_02_flux",
      "outcome": "allowed",
      "rule_trace": [
        {
          "rule_id": "REFERENCES_EXIST",
          "effect": "pass",
          "detail": "all referenced claims exist"
        },
        {
          "rule_id": "EPISTEMIC_STATUS_PRESERVED",
          "effect": "pass",
          "detail": "metaphorical treatment does not upgrade the claim"
        }
      ]
    }
  ]
}
```

`status` 只能是：

- `approved`：有一个候选可安全进入执行。
- `needs_policy_review`：偏好候选没有硬性驳回，但执行前需要人工判断。
- `no_safe_candidate`：所有候选都被驳回；不得向任何适配器发送任务。

每条规则结果具有四种效果之一：

- `pass`：无需动作。
- `warn`：保留在审计记录中，但不阻塞。
- `reject_candidate`：本流程内不可覆盖；继续评估下一个候选。
- `require_review`：停在当前候选并请求 Policy Review。

评估顺序是确定性的：

1. 按 `rank` 升序评估候选。
2. 同一候选中，`reject_candidate` 优先于 `require_review`；随后继续评估下一个候选。
3. 如果候选没有被驳回，但包含 `require_review`，以 `needs_policy_review` 停止。不能静默选择审美优先级更低的替代项。
4. 第一个只包含 `pass` 或 `warn` 的候选成为 `approved`。
5. 没有候选可用时返回 `no_safe_candidate`。

规则引擎必须为检查过的每个候选输出评估。`fallback_from` 按顺序列出已驳回的高优先级候选，使降级显式可见。

### 3.6 `PolicyReviewDecision` — 人工策略审核记录

```json
{
  "schema_version": 1,
  "review_id": "prev_neuro_012_02_a",
  "decision_id": "vadj_neuro_012_02_review",
  "proposal_id": "vp_neuro_012_02_a",
  "shot_id": "shot_neuro_012_02",
  "candidate_id": "cand_neuro_012_02_flux",
  "outcome": "approved",
  "reviewer_id": "local_reviewer_01",
  "rationale": "隐喻表达保留了不确定性，且没有补充证据之外的机制。",
  "reviewed_at": "2026-08-07T23:10:00Z"
}
```

`outcome` 是 `approved`、`rejected` 或 `revision_requested`。审核只能处理裁决中 `require_review` 的候选，不能批准带有 `reject_candidate` 的候选。`reviewer_id` 是稳定的本地审计标识；拒绝或要求修改的审核记录不能生成 Approved Visual Plan。

### 3.7 `ApprovedVisualPlan` — 唯一可供适配器消费的视觉指令

```json
{
  "schema_version": 1,
  "plan_id": "avp_neuro_012_02_a",
  "approval_ref": {
    "kind": "automatic_adjudication",
    "decision_id": "vadj_neuro_012_02_a"
  },
  "shot_id": "shot_neuro_012_02",
  "candidate_id": "cand_neuro_012_02_flux",
  "media_type": "generated_image",
  "visual_form": "concept_metaphor",
  "duration_ms": 4300,
  "purpose": "解释双曲空间为何适合表示快速扩张的神经连接",
  "visual_brief": "深蓝空间中神经节点由平面网格向外展开，明确主体与景深，无文字、标签、数字或水印。",
  "primary_claim_id": "hyperbolic_space_modeling",
  "supporting_claim_ids": [],
  "evidence_bindings": [],
  "evidence_presentation": "none",
  "epistemic_guardrails": {
    "kind": "claim_bound",
    "epistemic_status": "supported",
    "depiction_mode": "metaphorical",
    "must_not_imply": [
      "该几何模型就是大脑中的物理空间",
      "该模型已经解释意识的产生"
    ],
    "on_screen_qualifier": null
  },
  "continuity_intent": {
    "subject": "由规则网格转变为向外扩张的神经节点网络",
    "composition": "center_to_radial_expansion",
    "palette": "deep_blue_cyan",
    "shot_scale": "medium_to_wide",
    "transition_intent": "conceptual_reveal",
    "continuity_key": "hyperbolic_network_v1",
    "avoid_repetition_of": ["floating_brain", "generic_neuron_closeup"]
  },
  "presentation": {
    "framing": "cover",
    "background": "none",
    "motion": "slow_push_in",
    "source_audio": "not_applicable",
    "subtitle_mode": "ass_burn_in",
    "subtitle_track_id": "subs_neuro_zh_ass_v1"
  },
  "resolved_source": null
}
```

计划不变量：

- `approval_ref.kind` 为 `automatic_adjudication` 时引用 `approved` 裁决；为 `policy_review` 时同时引用原裁决和一条结果为 `approved` 的 PolicyReviewDecision。
- 非空 `primary_claim_id` 使用 `epistemic_guardrails.kind: claim_bound`，其中认识论状态来自 Director Brief，而不是模型自写的替代状态；空 `primary_claim_id` 只能使用 `{ "kind": "not_applicable" }`。
- `source_video` 必须有非空 `resolved_source`，其中来源已获授权，窗口完整、时长等于 Shot，且不超过来源引用上限。
- 非来源媒体必须使用 `resolved_source: null`。
- 它必须带有解析后的 `presentation`；面向适配器的 Prompt、工作流名称、文件路径和具体 FFmpeg 命令仍在此对象之后编译。

Policy Review 批准时使用可区分引用：

```json
{
  "kind": "policy_review",
  "decision_id": "vadj_neuro_012_02_review",
  "review_id": "prev_neuro_012_02_a"
}
```

### 3.8 解析来源窗口

`source_video` 使用独立的可信选择边界，因为语义正确和画面质量是两个问题。授权通过后，Source Selector 接收 Claim 的允许来源区间、Shot 时长、预期说话者或主体，以及可能存在的建议窗口。它返回窗口排名，并记录：

- 与 Claim 时间戳的语义距离；
- 预期说话者匹配度；
- 人脸可见性和发言者与画面人物的一致性；
- 镜头稳定性和边界完整性；
- 有效手势或运动强度；
- 主持人插话、PPT、黑帧、字幕遮挡关键内容或动作中途切断等惩罚项。

Source Selector 必须保存完整的 `SourceSelection`：

```json
{
  "schema_version": 1,
  "selection_id": "srcsel_neuro_004_01_a",
  "proposal_id": "vp_neuro_004_01_a",
  "candidate_id": "cand_neuro_004_01_source",
  "source_id": "interview_01",
  "source_fingerprint": "sha256:source-content-digest",
  "source_claim_id": "claim_consciousness_hypothesis",
  "expected_speaker_id": "speaker_guest_01",
  "intended_subject": "提出该观点的访谈嘉宾",
  "proposed_window": null,
  "allowed_region": {
    "start_ms": 912000,
    "end_ms": 925000
  },
  "requested_duration_ms": 4300,
  "scoring_profile": "source-window-v1",
  "minimum_score": 0.7,
  "tie_break_order": [
    "boundary_integrity",
    "semantic_proximity",
    "earlier_start_ms"
  ],
  "candidates": [
    {
      "window_id": "srcwin_neuro_004_01_a1",
      "window": {
        "start_ms": 913200,
        "end_ms": 917500
      },
      "component_scores": {
        "semantic_proximity": 0.91,
        "expected_speaker_match": 0.98,
        "face_visibility": 0.88,
        "speaker_screen_agreement": 0.95,
        "stability": 0.86,
        "boundary_integrity": 0.92,
        "useful_motion": 0.64
      },
      "penalties": [
        {
          "code": "subtitle_occlusion",
          "value": 0.04
        }
      ],
      "total_score": 0.84
    }
  ],
  "selected_window_id": "srcwin_neuro_004_01_a1"
}
```

`proposal_id` 和 `candidate_id` 固定选择请求的创意来源；`expected_speaker_id`、`intended_subject` 和原始 `proposed_window` 保存全部视觉检索输入。`scoring_profile` 是不可变的权重/归一化版本，使 `total_score` 可重放。返回的每个窗口都必须位于 `allowed_region` 内、与 `requested_duration_ms` 一致，并记录全部分项分数和惩罚。`selected_window_id` 必须指向最高合格候选；分数相同时按 `tie_break_order` 决定。

如果没有窗口达到 `minimum_score`，`selected_window_id` 为 null，候选进入 Policy Review；选择器不能静默扩大区域或换成无关画面。

成功解析后，`resolved_source` 使用以下形状：

```json
{
  "source_id": "interview_01",
  "window": {
    "start_ms": 913200,
    "end_ms": 917500
  },
  "authorization_context_id": "adjctx_neuro_004_01",
  "selection_id": "srcsel_neuro_004_01_a"
}
```

`window` 是完整的 `TimeRangeMs`；授权快照和选择结果都必须可追溯。

### 3.9 表现层计划

`presentation` 是规则层把候选意图解析成的媒介无关表现计划，至少包含：

- `framing`：`cover`、`contain_blurred_fill` 或 `template`；
- `background`：`none` 或 `blurred_source_fill`；
- `motion`：已批准的静止、原生运动、Ken Burns 缩放或平移意图；
- `source_audio`：`mute` 或 `not_applicable`；
- `subtitle_mode`：v1 固定为 `ass_burn_in`；
- `subtitle_track_id`：指向由 Qwen3-TTS 词级时间轴生成的可信 ASS 字幕轨。

当前默认值是：

| `media_type` | framing/background | motion | source audio | subtitle |
|---|---|---|---|---|
| `source_video` | `contain_blurred_fill` / `blurred_source_fill` | `native` | `mute` | `ass_burn_in` |
| `generated_image` | `cover` / `none` | Ken Burns 意图 | `not_applicable` | `ass_burn_in` |
| `generated_video` | `cover` / `none` | `native` | `not_applicable` | `ass_burn_in` |
| `programmatic_visual` | `cover` / `none` | `static` 或已批准慢动效 | `not_applicable` | `ass_burn_in` |
| `evidence_card` | `template` / `none` | `static` | `not_applicable` | `ass_burn_in` |

这层规定“如何呈现”，但不携带滤镜参数或命令行；`composition.py`/FFmpeg 在编译阶段把这些枚举转成具体实现。

### 3.10 `ReleaseDecision` — 最终发布批准

```json
{
  "schema_version": 1,
  "release_id": "release_neuro_preview_01",
  "composition_id": "composition_neuro_preview_01_r3",
  "outcome": "approved",
  "reviewer_id": "local_reviewer_01",
  "rationale": "证据表达、来源使用、节奏和画面质量均通过。",
  "reviewed_at": "2026-08-07T23:40:00Z"
}
```

`outcome` 是 `approved`、`changes_requested` 或 `rejected`。只有与当前 composition 修订版本精确匹配且结果为 `approved` 的 ReleaseDecision 才允许发布；重新合成后必须重新审核。

## 4. 强制裁决规则

规则按以下顺序应用。前面的硬性驳回不能被后续规则修复。

1. **Schema 与引用完整性**
   - ID 存在、排序有效、媒介专属字段一致，并且提案确实属于对应 Brief 和 Shot。

2. **权利与来源可用性**
   - 应用授权矩阵。
   - 驳回 `unknown` 权利状态。
   - 驳回超过 6 秒的来源片段。
   - `proposed_window` 为空时，只能在 Claim 的可信来源区域内解析窗口。

3. **证据与认识论安全**
   - 画面可以缩窄或限定 Claim，但不能强化它。
   - 数字、标签、坐标轴、百分比、日期和定量对比都必须通过 Evidence Binding 绑定具名 datum key。
   - 缺失证据时驳回相关候选；模型不能填补空缺。
   - 写实表达有争议或推测性机制时必须进入 Policy Review。
   - 所有非 `none` 的 `evidence_presentation` 都必须通过第 2.4 节合法性矩阵；除 `legacy_rules_v1` 的显式兼容原因外，普通事实不能仅凭 `fact_type` 强制打断节奏。

4. **时间**
   - 正常 Shot 以 4 秒为目标，并保持在 2.5–6 秒之间。
   - 为保留真实 TTS 对齐，`coarse_span` 可以超过 6 秒，但不能使用 `source_video`，也不能使用时长上限短于该 Shot 的生成器。
   - 裁决器绝不在词内或不透明 TTS 跨度中虚构切点。

5. **能力与预算**
   - 媒介不可用或预算耗尽时驳回该候选，并启动显式降级链。
   - H3 只是 `generated_video` 的一种可能适配器，不是由视觉导演作出的媒介决策。

6. **置信度与连续性**
   - 通过硬规则后，置信度低于配置阈值的候选必须进入 Policy Review。
   - 重复、色板冲突或镜头间对比不足通常产生 `warn`；严重的连续性歧义可以要求审核。
   - 全片媒介比例是诊断和审核信号，不是逐 Shot 的硬配额。

## 5. 现有路由规则的迁移

`legacy_rules_v1` 不裁决 Ollama 的 Proposal。版本化的确定性 Proposal Builder 以 `provenance.provider: rule_baseline` 构造完整 Visual Proposal，并按以下顺序写入候选 `rank`：

1. Beat 首个 Shot 存在本地来源 Claim，并且授权、窗口和 6 秒限制全部通过时，选择 `source_video`。
2. `factual` 或 `quotation` Beat 的最后一个 Shot 选择 `evidence_card`。如果 Beat 只有一个短 Shot 且第 1 条已选择来源视频，不再覆盖它。
3. 尚未选择媒介的 `counterpoint` Shot 使用 `visual_form: comparison`，当前生产路径为 `generated_image`。
4. 其余 Shot 使用 `generated_image`。

因此两种 profile 都执行第 3.5 节同一个按 `rank` 裁决的算法；区别只在于谁拥有排序。`legacy_rules_v1` 的 rank 由可信规则构造，`director_ranked_v1` 的 rank 来自 Ollama。

`director_ranked_v1` 的 Prompt 和 Schema 后置校验必须要求以下基线候选：首镜头来源候选、由可信 `evidence_display_reasons` 触发的证据呈现候选、Counterpoint 对比候选，以及解释镜头的低成本生成图片后备。如果模型漏掉必需候选或输出无效契约，该 Shot 不做候选注入或局部修补，而是整体改用一份新的 `rule_baseline` Proposal，并记录 `DIRECTOR_PROPOSAL_INVALID` 降级原因。

上线时先以 shadow 模式同时生成两份 Proposal，只把 `rule_baseline` Proposal 送入权威裁决，Ollama Proposal 仅记录差异。只有在来源使用、证据覆盖、降级率和人工驳回率达到约定阈值后，任务才切换到 `director_ranked_v1`。因此“复现当前行为”由 legacy Proposal Builder 保证，而不是依赖模型自觉排序。

## 6. 全片诊断

这些检查在 Shot 级裁决之后、发布之前运行，不会静默改写已批准计划。

- 标记连续超过三个 `media_type` 相同且 `visual_form` 相似的 Shot。
- 标记 `avoid_repetition_of` 中列出的重复主体或构图。
- 以默认 15%–30% 风格区间报告来源视频占比；不能仅为达到区间而加入原片。
- 分别报告全屏证据卡密度和轻量证据叠层密度。
- 标记连续出现的全屏证据卡。
- 对典型视频建议两到四个生成视频重点镜头；只有当运动能够传递信息时，才优先用于 Hook、核心机制和高潮。

当前 `comparison` 路由应同时按形式和媒介报告，例如 `comparison / generated_image`，从而量化未来向 `comparison / programmatic_visual` 的迁移。

## 7. 审核触发条件

以下情况必须进入 Policy Review：

- 当前候选的 `confidence` 低于策略阈值；
- 对有争议或推测性内容进行写实表达；
- Source Selector 无法在可信 Claim 区域内解析出视觉连贯的窗口；
- 候选会传达其 Claim 未覆盖的重要暗示；
- 连续性意图与相邻已批准 Shot 冲突，而确定性策略无法安全选择。

Release Review 始终检查：

- 审美质量和叙事节奏；
- 生成画面是否在结构检查通过后仍然夸大确定性；
- 来源片段的相关性和画面质量；
- 程序化图形和证据卡的可读性及来源链；
- 最终版权、平台和发布批准。

## 8. v1 明确不做的事

- 视觉导演不修改 Shot 边界。
- 它不决定来源权利或用户授权。
- 它不选择具体 FLUX、H3、ComfyUI、Pillow 或 FFmpeg 实现参数。
- 它不把数值事实复制进 Prompt。
- 它不自动发布，也不能免除 Release Review。
- 它不强制规定来源片段、证据卡、生成图片或生成视频的固定比例。

## 9. 请重点评审的决定

这份草案刻意作出以下八个选择；在添加可执行 Schema 前，需要明确接受或修改：

1. 权利元数据对视觉导演隐藏，只提供给裁决层。
2. 候选是有顺序的替代项；策略降级必须显式且可审计。
3. 遇到需要审核的偏好候选时停止降级；候选被硬性驳回时才启动降级。
4. `comparison` 是视觉形式，不是媒介类型。
5. 媒体适配器只消费 `ApprovedVisualPlan`；模型原始输出永远不可执行。
6. `legacy_rules_v1` 保证当前优先级，`director_ranked_v1` 才启用模型排序，并先经过 shadow 对比。
7. 证据呈现分为无提示、角标、来源条、全屏证据卡和不确定性卡，触发原因来自可信策略层。
8. 认识论表达和连续性意图属于各 Media Candidate，而不是整份 Proposal 的共享属性。

评审通过后，下一项变更应加入带鉴别字段的 Pydantic 模型、自动生成的 JSON Schema，以及位于 `model_validate` 公共边界的契约测试。运行时路由和 Ollama 调用仍是另一项独立变更。
