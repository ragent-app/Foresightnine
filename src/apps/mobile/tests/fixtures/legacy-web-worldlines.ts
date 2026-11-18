// Contract-shaped worldlines frozen from the retired Web UI fixtures on 2026-11-18.
import type { WorldlineBundle } from "@foresightnine/contracts";

export const legacyWorldlines: WorldlineBundle["worldlines"] = [
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "worldline_id": "wl-weak-web-e403b4a8",
    "bundle_id": "bundle-web-e403b4a8",
    "branch_kind": "weak",
    "title": "需求与投资延迟",
    "premise": "如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么电网设备与相关基础设施将受益。",
    "baseline_date": "2026-08-10",
    "horizon_end": "2028-08-31",
    "nodes": [
      {
        "node_id": "wl-weak-web-e403b4a8-0",
        "time_window": "现在",
        "event_date": "2026-08-10",
        "data_as_of": "2026-08-09",
        "event": "观点基线与需求信号",
        "observable_indicators": [
          "用户观点",
          "公开需求披露"
        ],
        "causal_parents": [],
        "state_changes": [
          "观察窗口开始"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-weak-web-e403b4a8-1",
        "time_window": "+6 个月",
        "event_date": "2027-02-10",
        "data_as_of": "2026-08-09",
        "event": "需求与投资指引走弱",
        "observable_indicators": [
          "投资计划",
          "设备订单"
        ],
        "causal_parents": [
          "wl-weak-web-e403b4a8-0"
        ],
        "state_changes": [
          "订单斜率下修"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-weak-web-e403b4a8-2",
        "time_window": "+12 个月",
        "event_date": "2027-08-10",
        "data_as_of": "2026-08-09",
        "event": "传导结果可见",
        "observable_indicators": [
          "订单交付",
          "利用率",
          "现金流"
        ],
        "causal_parents": [
          "wl-weak-web-e403b4a8-1"
        ],
        "state_changes": [
          "现金流验证"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-weak-web-e403b4a8-3",
        "time_window": "+24 个月",
        "event_date": "2028-08-10",
        "data_as_of": "2026-08-09",
        "event": "估值再定价或失效",
        "observable_indicators": [
          "利润兑现",
          "需求修正"
        ],
        "causal_parents": [
          "wl-weak-web-e403b4a8-2"
        ],
        "state_changes": [
          "路径结算"
        ],
        "confidence": "low"
      }
    ],
    "edges": [
      {
        "from_node_id": "wl-weak-web-e403b4a8-0",
        "to_node_id": "wl-weak-web-e403b4a8-1"
      },
      {
        "from_node_id": "wl-weak-web-e403b4a8-1",
        "to_node_id": "wl-weak-web-e403b4a8-2"
      },
      {
        "from_node_id": "wl-weak-web-e403b4a8-2",
        "to_node_id": "wl-weak-web-e403b4a8-3"
      }
    ],
    "asset_anchors": [
      "GRID",
      "CHIP",
      "UTIL"
    ],
    "asset_impacts": [],
    "event_reveal_schedule": [
      {
        "event_id": "wl-weak-web-e403b4a8-0",
        "event_date": "2026-08-10",
        "reveal_date": "2026-08-10",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-weak-web-e403b4a8-1",
        "event_date": "2027-02-10",
        "reveal_date": "2027-02-28",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-weak-web-e403b4a8-2",
        "event_date": "2027-08-10",
        "reveal_date": "2027-08-31",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-weak-web-e403b4a8-3",
        "event_date": "2028-08-10",
        "reveal_date": "2028-08-31",
        "rule": "scheduled"
      }
    ],
    "invalidation_conditions": [
      "需求与投资没有恢复",
      "资本开支指引连续下修"
    ],
    "provenance": "offline deterministic fixture; consumes user viewpoint; model estimate",
    "status": "WORLDLINE_READY"
  },
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "worldline_id": "wl-main-web-e403b4a8",
    "bundle_id": "bundle-web-e403b4a8",
    "branch_kind": "main",
    "title": "投资按计划兑现",
    "premise": "如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么电网设备与相关基础设施将受益。",
    "baseline_date": "2026-08-10",
    "horizon_end": "2028-08-31",
    "nodes": [
      {
        "node_id": "wl-main-web-e403b4a8-0",
        "time_window": "现在",
        "event_date": "2026-08-10",
        "data_as_of": "2026-08-09",
        "event": "观点基线与需求信号",
        "observable_indicators": [
          "用户观点",
          "公开需求披露"
        ],
        "causal_parents": [],
        "state_changes": [
          "观察窗口开始"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-main-web-e403b4a8-1",
        "time_window": "+6 个月",
        "event_date": "2027-02-10",
        "data_as_of": "2026-08-09",
        "event": "需求与投资指引确认",
        "observable_indicators": [
          "投资计划",
          "设备订单"
        ],
        "causal_parents": [
          "wl-main-web-e403b4a8-0"
        ],
        "state_changes": [
          "订单斜率改变"
        ],
        "confidence": "high"
      },
      {
        "node_id": "wl-main-web-e403b4a8-2",
        "time_window": "+12 个月",
        "event_date": "2027-08-10",
        "data_as_of": "2026-08-09",
        "event": "传导结果可见",
        "observable_indicators": [
          "订单交付",
          "利用率",
          "现金流"
        ],
        "causal_parents": [
          "wl-main-web-e403b4a8-1"
        ],
        "state_changes": [
          "现金流验证"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-main-web-e403b4a8-3",
        "time_window": "+24 个月",
        "event_date": "2028-08-10",
        "data_as_of": "2026-08-09",
        "event": "估值再定价或失效",
        "observable_indicators": [
          "利润兑现",
          "需求修正"
        ],
        "causal_parents": [
          "wl-main-web-e403b4a8-2"
        ],
        "state_changes": [
          "路径结算"
        ],
        "confidence": "low"
      }
    ],
    "edges": [
      {
        "from_node_id": "wl-main-web-e403b4a8-0",
        "to_node_id": "wl-main-web-e403b4a8-1"
      },
      {
        "from_node_id": "wl-main-web-e403b4a8-1",
        "to_node_id": "wl-main-web-e403b4a8-2"
      },
      {
        "from_node_id": "wl-main-web-e403b4a8-2",
        "to_node_id": "wl-main-web-e403b4a8-3"
      }
    ],
    "asset_anchors": [
      "GRID",
      "CHIP",
      "UTIL"
    ],
    "asset_impacts": [],
    "event_reveal_schedule": [
      {
        "event_id": "wl-main-web-e403b4a8-0",
        "event_date": "2026-08-10",
        "reveal_date": "2026-08-10",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-main-web-e403b4a8-1",
        "event_date": "2027-02-10",
        "reveal_date": "2027-02-28",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-main-web-e403b4a8-2",
        "event_date": "2027-08-10",
        "reveal_date": "2027-08-31",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-main-web-e403b4a8-3",
        "event_date": "2028-08-10",
        "reveal_date": "2028-08-31",
        "rule": "scheduled"
      }
    ],
    "invalidation_conditions": [
      "需求增长低于用户观点中的基线",
      "资本开支指引连续下修"
    ],
    "provenance": "offline deterministic fixture; consumes user viewpoint; model estimate",
    "status": "WORLDLINE_READY"
  },
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "worldline_id": "wl-strong-web-e403b4a8",
    "bundle_id": "bundle-web-e403b4a8",
    "branch_kind": "strong",
    "title": "政策与融资共振",
    "premise": "如果未来两年全球数据中心电力需求持续上升，并推动电网设备投资提速，那么电网设备与相关基础设施将受益。",
    "baseline_date": "2026-08-10",
    "horizon_end": "2028-08-31",
    "nodes": [
      {
        "node_id": "wl-strong-web-e403b4a8-0",
        "time_window": "现在",
        "event_date": "2026-08-10",
        "data_as_of": "2026-08-09",
        "event": "观点基线与需求信号",
        "observable_indicators": [
          "用户观点",
          "公开需求披露"
        ],
        "causal_parents": [],
        "state_changes": [
          "观察窗口开始"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-strong-web-e403b4a8-1",
        "time_window": "+6 个月",
        "event_date": "2027-02-10",
        "data_as_of": "2026-08-09",
        "event": "政策与融资提前加速",
        "observable_indicators": [
          "投资计划",
          "设备订单"
        ],
        "causal_parents": [
          "wl-strong-web-e403b4a8-0"
        ],
        "state_changes": [
          "订单斜率改变"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-strong-web-e403b4a8-2",
        "time_window": "+12 个月",
        "event_date": "2027-08-10",
        "data_as_of": "2026-08-09",
        "event": "传导结果可见",
        "observable_indicators": [
          "订单交付",
          "利用率",
          "现金流"
        ],
        "causal_parents": [
          "wl-strong-web-e403b4a8-1"
        ],
        "state_changes": [
          "现金流验证"
        ],
        "confidence": "medium"
      },
      {
        "node_id": "wl-strong-web-e403b4a8-3",
        "time_window": "+24 个月",
        "event_date": "2028-08-10",
        "data_as_of": "2026-08-09",
        "event": "估值再定价或失效",
        "observable_indicators": [
          "利润兑现",
          "需求修正"
        ],
        "causal_parents": [
          "wl-strong-web-e403b4a8-2"
        ],
        "state_changes": [
          "路径结算"
        ],
        "confidence": "low"
      }
    ],
    "edges": [
      {
        "from_node_id": "wl-strong-web-e403b4a8-0",
        "to_node_id": "wl-strong-web-e403b4a8-1"
      },
      {
        "from_node_id": "wl-strong-web-e403b4a8-1",
        "to_node_id": "wl-strong-web-e403b4a8-2"
      },
      {
        "from_node_id": "wl-strong-web-e403b4a8-2",
        "to_node_id": "wl-strong-web-e403b4a8-3"
      }
    ],
    "asset_anchors": [
      "GRID",
      "CHIP",
      "UTIL"
    ],
    "asset_impacts": [],
    "event_reveal_schedule": [
      {
        "event_id": "wl-strong-web-e403b4a8-0",
        "event_date": "2026-08-10",
        "reveal_date": "2026-08-10",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-strong-web-e403b4a8-1",
        "event_date": "2027-02-10",
        "reveal_date": "2027-02-28",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-strong-web-e403b4a8-2",
        "event_date": "2027-08-10",
        "reveal_date": "2027-08-31",
        "rule": "scheduled"
      },
      {
        "event_id": "wl-strong-web-e403b4a8-3",
        "event_date": "2028-08-10",
        "reveal_date": "2028-08-31",
        "rule": "scheduled"
      }
    ],
    "invalidation_conditions": [
      "需求增长低于用户观点中的基线",
      "融资收紧或政策约束"
    ],
    "provenance": "offline deterministic fixture; consumes user viewpoint; model estimate",
    "status": "WORLDLINE_READY"
  }
];

export const legacyProbabilityAssessments: WorldlineBundle["probability_assessments"] = [
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "assessment_id": "assessment-weak-e403b4a8",
    "worldline_id": "wl-weak-web-e403b4a8",
    "premise_probability": 0.42,
    "premise_probability_range": {
      "minimum": 0.35,
      "maximum": 0.49
    },
    "conditional_probability": 0.27,
    "conditional_probability_range": {
      "minimum": 0.2,
      "maximum": 0.35
    },
    "joint_probability": 0.1134,
    "joint_probability_range": {
      "minimum": 0.06999999999999999,
      "maximum": 0.17149999999999999
    },
    "confidence": "medium",
    "base_rate_refs": [],
    "adjustment_reasons": [
      "支持证据向上",
      "反对证据向下",
      "模型不确定性：区间加宽"
    ],
    "calibration_status": "model_estimate_unvalidated"
  },
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "assessment_id": "assessment-main-e403b4a8",
    "worldline_id": "wl-main-web-e403b4a8",
    "premise_probability": 0.42,
    "premise_probability_range": {
      "minimum": 0.35,
      "maximum": 0.49
    },
    "conditional_probability": 0.54,
    "conditional_probability_range": {
      "minimum": 0.45,
      "maximum": 0.62
    },
    "joint_probability": 0.2268,
    "joint_probability_range": {
      "minimum": 0.1575,
      "maximum": 0.3038
    },
    "confidence": "medium",
    "base_rate_refs": [],
    "adjustment_reasons": [
      "支持证据向上",
      "反对证据向下",
      "模型不确定性：区间加宽"
    ],
    "calibration_status": "model_estimate_unvalidated"
  },
  {
    "schema_version": "v1",
    "created_at": "2026-08-10T00:00:00Z",
    "source_refs": [
      {
        "ref_id": "fixture:web",
        "source_type": "model_inference",
        "label": "Web offline fixture"
      }
    ],
    "content_hash": "fixture-web",
    "assessment_id": "assessment-strong-e403b4a8",
    "worldline_id": "wl-strong-web-e403b4a8",
    "premise_probability": 0.42,
    "premise_probability_range": {
      "minimum": 0.35,
      "maximum": 0.49
    },
    "conditional_probability": 0.19,
    "conditional_probability_range": {
      "minimum": 0.12,
      "maximum": 0.25
    },
    "joint_probability": 0.0798,
    "joint_probability_range": {
      "minimum": 0.041999999999999996,
      "maximum": 0.1225
    },
    "confidence": "medium",
    "base_rate_refs": [],
    "adjustment_reasons": [
      "支持证据向上",
      "反对证据向下",
      "模型不确定性：区间加宽"
    ],
    "calibration_status": "model_estimate_unvalidated"
  }
];
