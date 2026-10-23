"""scanner 包：扫描深度增强。

三层能力（对应"不够深"的补课）：
  · 深度检测：detectors（77 条结构化规则）+ entropy（香农熵兜底未知形态）
  · 运营化  ：findings（指纹/去重/三档抑制/基线）+ engine（遍历 + diff-aware 增量）
  · 生态互通：sarif（SARIF 2.1.0，可直投 GitHub Code Scanning / IDE）
另有：semgrep_adapter（AST 级可选升级）、validators（活体验证）、push_protection（CI 闸门）。
"""

from scanner.detectors import (DETECTORS, BY_ID, Detector, detector_stats,
                               find_in_text, select)
from scanner.entropy import (shannon_entropy, sliding_window_entropy, entropy_score,
                             assess, extract_high_entropy_candidates,
                             has_secret_context, hit_stopword)
from scanner.findings import (Finding, Allowlist, confidence_of, redact, fingerprint,
                              normalize, dedupe, apply_suppressions, has_inline_allow,
                              load_ignore_file, save_baseline, load_baseline,
                              diff_against_baseline, changed_files, summarize, gate)
from scanner.engine import (scan_path, scan_file, iter_files, DEFAULT_MAX_BYTES)
from scanner.sarif import (to_sarif, to_sarif_json, write_sarif, validate_sarif,
                           build_rules, SARIF_VERSION)
from scanner.semgrep_adapter import semgrep_available, scan_with_semgrep
from scanner.validators import validate_secret, validate_findings, GitHubTokenValidator
from scanner.push_protection import generate_pre_push_hook, github_actions_yaml

__all__ = [
    # 深度检测
    "DETECTORS", "BY_ID", "Detector", "detector_stats", "find_in_text", "select",
    "shannon_entropy", "sliding_window_entropy", "entropy_score", "assess",
    "extract_high_entropy_candidates", "has_secret_context", "hit_stopword",
    # 运营化
    "Finding", "Allowlist", "confidence_of", "redact", "fingerprint", "normalize",
    "dedupe", "apply_suppressions", "has_inline_allow", "load_ignore_file",
    "save_baseline", "load_baseline", "diff_against_baseline", "changed_files",
    "summarize", "gate", "scan_path", "scan_file", "iter_files", "DEFAULT_MAX_BYTES",
    # 生态
    "to_sarif", "to_sarif_json", "write_sarif", "validate_sarif", "build_rules",
    "SARIF_VERSION",
    # 既有
    "semgrep_available", "scan_with_semgrep",
    "validate_secret", "validate_findings", "GitHubTokenValidator",
    "generate_pre_push_hook", "github_actions_yaml",
]
