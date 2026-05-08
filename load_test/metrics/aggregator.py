"""Statistical aggregation and comparison of test results."""

from dataclasses import dataclass, field

from .collector import TestRunMetrics
from .system_monitor import ResourceSnapshot

# Two-stage survivorship guard:
#
# 1) Absolute floor: a session must have produced at least this many frames to
#    contribute gap/silence samples. Keeps out "server gave me 5 frames and
#    disconnected" cases whose sparse, well-paced gaps would be misleading.
#
# 2) Relative-to-max: a session must have produced at least this fraction of
#    the run's best-producing session. Self-calibrates per run: when most
#    sessions are healthy the threshold is high and outliers are excluded;
#    when most sessions are broken the threshold is low and we include what
#    we have (but the with_output vs eligible counts in the terminal warn
#    the reader that the stats are survivor-biased).
#
# Numbers beyond these two guards are suspicious — the print_single_summary
# annotates "if eligible << with_output, survivorship bias is in play".
MIN_FRAMES_ABSOLUTE = 100
MIN_FRAMES_RELATIVE_TO_MAX = 0.30


@dataclass
class LatencyStats:
    count: int = 0
    mean: float = 0.0
    median: float = 0.0
    std_dev: float = 0.0
    min: float = 0.0
    max: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0


@dataclass
class ResourceStats:
    mean_cpu: float = 0.0
    peak_cpu: float = 0.0
    mean_memory_mb: float = 0.0
    peak_memory_mb: float = 0.0


@dataclass
class RunSummary:
    """Aggregated results for one test run."""
    implementation: str = ""
    concurrency: int = 0
    total_sessions: int = 0
    total_frames_sent: int = 0
    total_frames_received: int = 0
    output_to_input_frame_ratio: float = 0.0
    throughput_fps: float = 0.0

    # Cold-start first-frame (session start → first received frame)
    first_frame_latency: LatencyStats = field(default_factory=LatencyStats)
    # Post-warmup steady-state RTT (first post-reset send → first post-reset recv)
    post_warmup_rtt: LatencyStats = field(default_factory=LatencyStats)
    rtt_latency: LatencyStats = field(default_factory=LatencyStats)
    jitter: LatencyStats = field(default_factory=LatencyStats)
    within_phrase_gap: LatencyStats = field(default_factory=LatencyStats)
    # Audible silence gap = max(0, within_phrase_gap − pacing_interval_sec).
    # This is the time the listener's playback buffer could starve beyond the
    # expected per-frame wait. p99 > ~5 ms is the threshold for "audible on phone."
    audible_silence_gap: LatencyStats = field(default_factory=LatencyStats)
    # Pacing interval used when deriving audible_silence_gap (0.040 for pipecat,
    # 0.020 for agent-transport). Persisted so readers can re-derive silence.
    pacing_interval_sec: float = 0.0
    resources: ResourceStats = field(default_factory=ResourceStats)

    # Component latencies
    transport_delivery: LatencyStats = field(default_factory=LatencyStats)
    pipeline_latency: LatencyStats = field(default_factory=LatencyStats)

    # Survivorship-bias accountability (see summarize_run)
    sessions_with_output: int = 0      # sessions that produced ≥1 received frame
    jitter_eligible_sessions: int = 0  # sessions contributing to jitter stats


@dataclass
class ComparisonResult:
    """Side-by-side comparison of two implementations."""
    baseline: RunSummary = field(default_factory=RunSummary)
    candidate: RunSummary = field(default_factory=RunSummary)

    @property
    def rtt_p50_improvement_pct(self) -> float:
        return _pct_change(self.baseline.rtt_latency.p50, self.candidate.rtt_latency.p50)

    @property
    def rtt_p99_improvement_pct(self) -> float:
        return _pct_change(self.baseline.rtt_latency.p99, self.candidate.rtt_latency.p99)

    @property
    def jitter_improvement_pct(self) -> float:
        return _pct_change(self.baseline.jitter.p50, self.candidate.jitter.p50)

    @property
    def cpu_reduction_pct(self) -> float:
        return _pct_change(self.baseline.resources.mean_cpu, self.candidate.resources.mean_cpu)

    @property
    def memory_reduction_pct(self) -> float:
        return _pct_change(self.baseline.resources.peak_memory_mb, self.candidate.resources.peak_memory_mb)


def compute_latency_stats(values: list[float]) -> LatencyStats:
    """Compute percentile statistics from a list of latency values (seconds)."""
    if not values:
        return LatencyStats()

    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mean = sum(sorted_vals) / n
    variance = sum((v - mean) ** 2 for v in sorted_vals) / n

    return LatencyStats(
        count=n,
        mean=mean,
        median=_percentile(sorted_vals, 50),
        std_dev=variance ** 0.5,
        min=sorted_vals[0],
        max=sorted_vals[-1],
        p50=_percentile(sorted_vals, 50),
        p75=_percentile(sorted_vals, 75),
        p90=_percentile(sorted_vals, 90),
        p95=_percentile(sorted_vals, 95),
        p99=_percentile(sorted_vals, 99),
    )


def compute_resource_stats(snapshots: list[ResourceSnapshot]) -> ResourceStats:
    if not snapshots:
        return ResourceStats()

    cpus = [s.cpu_percent for s in snapshots]
    mems = [s.memory_mb for s in snapshots]
    return ResourceStats(
        mean_cpu=sum(cpus) / len(cpus),
        peak_cpu=max(cpus),
        mean_memory_mb=sum(mems) / len(mems),
        peak_memory_mb=max(mems),
    )


def summarize_run(
    run: TestRunMetrics,
    resource_snapshots: list[ResourceSnapshot] | None = None,
    pacing_interval_sec: float = 0.020,
    min_frames_absolute: int = MIN_FRAMES_ABSOLUTE,
    min_frames_relative_to_max: float = MIN_FRAMES_RELATIVE_TO_MAX,
) -> RunSummary:
    """Aggregate all session metrics into a single RunSummary.

    Args:
        pacing_interval_sec: Expected per-frame delivery interval for this
            implementation (0.040 for pipecat, 0.020 for agent-transport).
            Used to derive audible_silence_gap.
        min_frames_absolute: Sessions with fewer than this many received
            frames are excluded from gap/silence/jitter stats (survivorship
            guard — a session that got 10 well-paced frames before dying is
            not representative of what a real user experienced).
        min_frames_relative_to_max: Sessions must have received at least this
            fraction of the best-producing session's frame count to qualify.
            Self-calibrates per run: tight distributions barely filter
            anything; wide distributions (some dead, some alive) exclude the
            dead tail.
    """
    all_rtts: list[float] = []
    all_within_phrase_gaps: list[float] = []
    all_first_frame: list[float] = []
    all_post_warmup_rtt: list[float] = []
    all_transport_delivery: list[float] = []
    all_pipeline_latency: list[float] = []
    total_sent = 0
    total_received = 0
    sessions_with_output = 0
    jitter_eligible_sessions = 0

    # First pass: collect per-session frame counts to compute the relative
    # threshold (needed before we can decide eligibility).
    session_list = list(run.sessions.values())
    max_frames_any_session = max((sm.frames_received for sm in session_list), default=0)
    relative_floor = int(max_frames_any_session * min_frames_relative_to_max)
    eligibility_threshold = max(min_frames_absolute, relative_floor)

    for sm in session_list:
        all_rtts.extend(sm.round_trip_latencies)
        all_transport_delivery.extend(sm.transport_delivery_times)
        all_pipeline_latency.extend(sm.pipeline_latencies)
        total_sent += sm.frames_sent
        total_received += sm.frames_received

        if sm.frames_received > 0:
            sessions_with_output += 1

        # Two-stage survivorship guard (see module-level constants).
        if sm.frames_received >= eligibility_threshold:
            all_within_phrase_gaps.extend(sm.within_phrase_gaps)
            jitter_eligible_sessions += 1

        if sm.first_frame_latency > 0:
            all_first_frame.append(sm.first_frame_latency)
        if sm.post_warmup_rtt > 0:
            all_post_warmup_rtt.append(sm.post_warmup_rtt)

    # Compute jitter as deviation from MEAN gap (not a hardcoded baseline).
    # Perfect pacing → all gaps identical → deviations = 0.
    if all_within_phrase_gaps:
        mean_gap = sum(all_within_phrase_gaps) / len(all_within_phrase_gaps)
        jitter_values = [abs(g - mean_gap) for g in all_within_phrase_gaps]
    else:
        jitter_values = []

    # Audible silence = the part of the gap that exceeds the expected pacing
    # interval. A listener's jitter buffer absorbs delays up to this interval;
    # anything beyond is what they might actually hear as a break.
    silence_values = [max(0.0, g - pacing_interval_sec) for g in all_within_phrase_gaps]

    output_to_input_ratio = (total_received / total_sent) if total_sent > 0 else 0.0
    throughput = total_received / run.wall_duration if run.wall_duration > 0 else 0.0

    snapshots = resource_snapshots or []
    if run.wall_start > 0 and run.wall_end > 0:
        steady_state_snapshots = [
            snap for snap in snapshots if run.wall_start <= snap.timestamp <= run.wall_end
        ]
        if steady_state_snapshots:
            snapshots = steady_state_snapshots

    return RunSummary(
        implementation=run.implementation,
        concurrency=run.concurrency,
        total_sessions=len(run.sessions),
        total_frames_sent=total_sent,
        total_frames_received=total_received,
        output_to_input_frame_ratio=output_to_input_ratio,
        throughput_fps=throughput,
        first_frame_latency=compute_latency_stats(all_first_frame),
        post_warmup_rtt=compute_latency_stats(all_post_warmup_rtt),
        rtt_latency=compute_latency_stats(all_rtts),
        jitter=compute_latency_stats(jitter_values),
        within_phrase_gap=compute_latency_stats(all_within_phrase_gaps),
        audible_silence_gap=compute_latency_stats(silence_values),
        pacing_interval_sec=pacing_interval_sec,
        resources=compute_resource_stats(snapshots),
        transport_delivery=compute_latency_stats(all_transport_delivery),
        pipeline_latency=compute_latency_stats(all_pipeline_latency),
        sessions_with_output=sessions_with_output,
        jitter_eligible_sessions=jitter_eligible_sessions,
    )


def compare_runs(baseline: RunSummary, candidate: RunSummary) -> ComparisonResult:
    return ComparisonResult(baseline=baseline, candidate=candidate)


def _percentile(sorted_data: list[float], p: float) -> float:
    """Compute percentile from pre-sorted data."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def _pct_change(baseline: float, candidate: float) -> float:
    """Percentage change from baseline to candidate. Negative = improvement."""
    if baseline == 0:
        return 0.0
    return ((candidate - baseline) / baseline) * 100
