"""Terminal and JSON reporting for load test results."""

import json
import sys
from dataclasses import asdict

from load_test.metrics.aggregator import ComparisonResult, RunSummary


def _ms(seconds: float) -> str:
    """Format seconds as milliseconds with 2 decimal places."""
    return f"{seconds * 1000:.2f}"


def _maybe_ms(seconds: float) -> str:
    if seconds <= 0:
        return "--"
    return f"{seconds * 1000:.2f}"


def _maybe_num(value: float, unit: str = "", decimals: int = 1) -> str:
    if value <= 0:
        return "--"
    suffix = f" {unit}" if unit else ""
    return f"{value:.{decimals}f}{suffix}"


def _pct(value: float) -> str:
    """Format percentage with sign."""
    if value == 0:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}%"


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _is_tty() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _is_tty() else s


def _color_delta(value: float) -> str:
    """Color the delta: green if negative (improvement), red if positive (regression)."""
    text = _pct(value)
    if value < 0:
        return _green(text)
    elif value > 0:
        return _red(text)
    return text


def print_single_summary(summary: RunSummary):
    """Print results for a single implementation.

    Leads with the metrics that drive our decisions: audible silence gap and
    CPU. Survivorship counters (with_output / jitter_eligible) are shown next
    to total sessions so a collapsed server can't hide behind clean-looking
    percentiles.
    """
    pacing_ms = summary.pacing_interval_sec * 1000
    expected_fps = 1.0 / summary.pacing_interval_sec if summary.pacing_interval_sec > 0 else 0
    print(f"\n{'=' * 60}")
    print(f"  {summary.implementation} | concurrency={summary.concurrency} | "
          f"pacing={pacing_ms:.0f} ms ({expected_fps:.0f} f/s ideal)")
    print(f"  sessions={summary.total_sessions} | "
          f"with_output={summary.sessions_with_output} | "
          f"eligible={summary.jitter_eligible_sessions}"
          f"  ← if eligible << with_output, survivorship bias is in play")
    print(f"  frames: {summary.total_frames_sent} sent / {summary.total_frames_received} recv | "
          f"throughput={summary.throughput_fps:.1f} f/s")
    print(f"{'=' * 60}")

    rows = [
        # PRIMARY: what we actually care about
        ("Audible silence gap", f"{_maybe_ms(summary.audible_silence_gap.p50)} ms", f"{_maybe_ms(summary.audible_silence_gap.p99)} ms"),
        ("  (max)",             f"{_maybe_ms(summary.audible_silence_gap.max)} ms", ""),
        ("Mean / peak CPU",     _maybe_num(summary.resources.mean_cpu, "%"), _maybe_num(summary.resources.peak_cpu, "%")),
        # SECONDARY: useful but not decision-drivers
        ("Within-phrase gap",   f"{_maybe_ms(summary.within_phrase_gap.mean)} ms", f"{_maybe_ms(summary.within_phrase_gap.p99)} ms"),
        ("Jitter (p50/p99)",    f"{_maybe_ms(summary.jitter.p50)} ms", f"{_maybe_ms(summary.jitter.p99)} ms"),
        ("First-frame (cold)",  f"{_maybe_ms(summary.first_frame_latency.p50)} ms", f"{_maybe_ms(summary.first_frame_latency.p99)} ms"),
        ("Post-warmup RTT",     f"{_maybe_ms(summary.post_warmup_rtt.p50)} ms", f"{_maybe_ms(summary.post_warmup_rtt.p99)} ms"),
        ("Pipeline (STT+LLM+TTS)", f"{_maybe_ms(summary.pipeline_latency.p50)} ms", f"{_maybe_ms(summary.pipeline_latency.p99)} ms"),
        ("Mean / peak memory",  _maybe_num(summary.resources.mean_memory_mb, "MB"), _maybe_num(summary.resources.peak_memory_mb, "MB")),
    ]

    print(f"\n  {'Metric':<25} {'p50/mean':>15} {'p99/peak':>15}")
    print(f"  {'-' * 25} {'-' * 15} {'-' * 15}")
    for label, v1, v2 in rows:
        print(f"  {label:<25} {v1:>15} {v2:>15}")
    sys.stdout.flush()


def print_comparison(comparison: ComparisonResult):
    """Print side-by-side comparison of two implementations."""
    b = comparison.baseline
    c = comparison.candidate

    # Use implementation names from the summaries (dynamic, not hardcoded)
    b_name = b.implementation or "Baseline"
    c_name = c.implementation or "Candidate"
    b_width = max(15, len(b_name) + 2)
    c_width = max(17, len(c_name) + 2)

    print(f"\n{'=' * (30 + b_width + c_width + 12)}")
    print(f"  Benchmark Comparison | concurrency={b.concurrency}")
    print(f"{'=' * (30 + b_width + c_width + 12)}")

    header = f"  {'Metric':<25} {b_name:>{b_width}} {c_name:>{c_width}} {'Delta':>12}"
    divider = f"  {'-' * 25} {'-' * b_width} {'-' * c_width} {'-' * 12}"

    rows = [
        # PRIMARY — these are what the decision hinges on
        ("Audible silence p50", _maybe_ms(b.audible_silence_gap.p50), _maybe_ms(c.audible_silence_gap.p50), "ms"),
        ("Audible silence p99", _maybe_ms(b.audible_silence_gap.p99), _maybe_ms(c.audible_silence_gap.p99), "ms"),
        ("Audible silence max", _maybe_ms(b.audible_silence_gap.max), _maybe_ms(c.audible_silence_gap.max), "ms"),
        ("Mean CPU", _maybe_num(b.resources.mean_cpu), _maybe_num(c.resources.mean_cpu), "%"),
        ("Peak CPU", _maybe_num(b.resources.peak_cpu), _maybe_num(c.resources.peak_cpu), "%"),
        # Survivorship guardrails — if these diverge, the above numbers may be biased
        ("Output throughput", f"{b.throughput_fps:.1f}", f"{c.throughput_fps:.1f}", "f/s"),
        ("Sessions with output", str(b.sessions_with_output), str(c.sessions_with_output), ""),
        ("Eligible sessions", str(b.jitter_eligible_sessions), str(c.jitter_eligible_sessions), ""),
        # Secondary
        ("Within-phrase gap", _maybe_ms(b.within_phrase_gap.mean), _maybe_ms(c.within_phrase_gap.mean), "ms"),
        ("Jitter (p50)", _maybe_ms(b.jitter.p50), _maybe_ms(c.jitter.p50), "ms"),
        ("Jitter (p99)", _maybe_ms(b.jitter.p99), _maybe_ms(c.jitter.p99), "ms"),
        ("First-frame cold (p50)", _maybe_ms(b.first_frame_latency.p50), _maybe_ms(c.first_frame_latency.p50), "ms"),
        ("Post-warmup RTT (p50)", _maybe_ms(b.post_warmup_rtt.p50), _maybe_ms(c.post_warmup_rtt.p50), "ms"),
        ("Pipeline STT+LLM+TTS", _maybe_ms(b.pipeline_latency.p50), _maybe_ms(c.pipeline_latency.p50), "ms"),
        ("Peak memory", _maybe_num(b.resources.peak_memory_mb), _maybe_num(c.resources.peak_memory_mb), "MB"),
    ]

    print(header)
    print(divider)
    for label, bval, cval, unit in rows:
        # Compute delta
        try:
            bf = float(bval)
            cf = float(cval)
            if bf > 0:
                delta_pct = ((cf - bf) / bf) * 100
                delta_str = _color_delta(delta_pct)
            else:
                delta_str = "--"
        except ValueError:
            delta_str = "--"

        print(f"  {label:<25} {bval + ' ' + unit:>{b_width}} {cval + ' ' + unit:>{c_width}} {delta_str:>12}")

    print()
    sys.stdout.flush()


def export_json(summaries: list[RunSummary], comparisons: list[ComparisonResult], path: str):
    """Write results to a JSON file."""
    data = {
        "summaries": [_summary_to_dict(s) for s in summaries],
        "comparisons": [
            {
                "baseline": _summary_to_dict(c.baseline),
                "candidate": _summary_to_dict(c.candidate),
            }
            for c in comparisons
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Results exported to {path}")
    sys.stdout.flush()


def _summary_to_dict(s: RunSummary) -> dict:
    return {
        "implementation": s.implementation,
        "concurrency": s.concurrency,
        "pacing_interval_sec": s.pacing_interval_sec,
        "total_sessions": s.total_sessions,
        "sessions_with_output": s.sessions_with_output,
        "jitter_eligible_sessions": s.jitter_eligible_sessions,
        "total_frames_sent": s.total_frames_sent,
        "total_frames_received": s.total_frames_received,
        "output_to_input_frame_ratio": s.output_to_input_frame_ratio,
        "throughput_fps": s.throughput_fps,
        "first_frame_latency": asdict(s.first_frame_latency),
        "post_warmup_rtt": asdict(s.post_warmup_rtt),
        "rtt_latency": asdict(s.rtt_latency),
        "jitter": asdict(s.jitter),
        "within_phrase_gap": asdict(s.within_phrase_gap),
        "audible_silence_gap": asdict(s.audible_silence_gap),
        "transport_delivery": asdict(s.transport_delivery),
        "pipeline_latency": asdict(s.pipeline_latency),
        "resources": asdict(s.resources),
    }
