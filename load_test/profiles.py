"""Load test profile definitions."""

from dataclasses import dataclass


@dataclass
class LoadStep:
    concurrency: int
    duration_sec: float
    ramp_delay: float  # delay between starting each session (seconds)
    warmup_sec: float = 0  # seconds to run before measuring (discard early metrics)


@dataclass
class LoadProfile:
    name: str
    steps: list[LoadStep]
    # Restart the target container between steps. Each concurrency step then
    # runs against a freshly-started server process (model caches cold, allocator
    # pristine, no leftover state from prior steps). Slower (~10-15s per step for
    # healthcheck + prewarm), but gives independent measurements.
    fresh_container_per_step: bool = False


PROFILES: dict[str, LoadProfile] = {
    "smoke": LoadProfile("smoke", [
        LoadStep(concurrency=1, duration_sec=10, ramp_delay=0),
    ]),
    "light": LoadProfile("light", [
        LoadStep(concurrency=5, duration_sec=30, ramp_delay=0.5),
    ]),
    "medium": LoadProfile("medium", [
        LoadStep(concurrency=10, duration_sec=30, ramp_delay=0.2),
        LoadStep(concurrency=25, duration_sec=30, ramp_delay=0.1),
    ]),
    "heavy": LoadProfile("heavy", [
        LoadStep(concurrency=10, duration_sec=20, ramp_delay=0.1),
        LoadStep(concurrency=25, duration_sec=30, ramp_delay=0.1),
        LoadStep(concurrency=50, duration_sec=30, ramp_delay=0.05),
    ]),
    "stress": LoadProfile("stress", [
        LoadStep(concurrency=10, duration_sec=15, ramp_delay=0.1),
        LoadStep(concurrency=25, duration_sec=15, ramp_delay=0.1),
        LoadStep(concurrency=50, duration_sec=20, ramp_delay=0.05),
        LoadStep(concurrency=100, duration_sec=30, ramp_delay=0.02),
    ]),
    "capacity": LoadProfile("capacity", [
        # Capacity probing around the 40-call knee:
        # shorter warmup at 20/30, then longer holds at 40/50/60
        # so CPU saturation and audio-quality drift have time to appear.
        LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.10),
        LoadStep(concurrency=30, duration_sec=60, ramp_delay=0.08),
        LoadStep(concurrency=40, duration_sec=90, ramp_delay=0.05),
        LoadStep(concurrency=50, duration_sec=90, ramp_delay=0.05),
        LoadStep(concurrency=60, duration_sec=90, ramp_delay=0.05),
    ]),
    "custom": LoadProfile(
        name="custom",
        steps=[
            LoadStep(concurrency=10, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.08, warmup_sec=10),
            LoadStep(concurrency=30, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
        ],
        # Each concurrency step gets a freshly-restarted container so results
        # are independent (no warm caches bleeding from prior steps).
        fresh_container_per_step=True,
    ),
    # For probing Python-VAD scaling cliff — stops at c=50 where it tends to choke
    "pyvad_probe": LoadProfile("pyvad_probe", [
        LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.08, warmup_sec=10),
        LoadStep(concurrency=30, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
    ]),
    # Capacity probe for AT + Rust VAD past the c=30 point where Python-VAD
    # configs collapse. 60 s steady state per step, fresh container per step.
    "at_rust_probe": LoadProfile(
        name="at_rust_probe",
        steps=[
            LoadStep(concurrency=40, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=60, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=70, duration_sec=60, ramp_delay=0.04, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Follow-up probes: push past the previously-reported cliffs now that we know
    # memory pressure was masking real ceilings.
    "at_py_90100": LoadProfile(
        name="at_py_90100",
        steps=[
            LoadStep(concurrency=90,  duration_sec=60, ramp_delay=0.035, warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03,  warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Full c=10..100 sweep in one profile (validation re-run).
    "scratch_10100": LoadProfile(
        name="scratch_10100",
        steps=[
            LoadStep(concurrency=10,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=20,  duration_sec=60, ramp_delay=0.08, warmup_sec=10),
            LoadStep(concurrency=30,  duration_sec=60, ramp_delay=0.07, warmup_sec=10),
            LoadStep(concurrency=40,  duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=50,  duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=60,  duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=70,  duration_sec=60, ramp_delay=0.04, warmup_sec=10),
            LoadStep(concurrency=80,  duration_sec=60, ramp_delay=0.04, warmup_sec=10),
            LoadStep(concurrency=90,  duration_sec=60, ramp_delay=0.035, warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Scratch push even higher — c=225/250/275/300.
    "scratch_225300": LoadProfile(
        name="scratch_225300",
        steps=[
            LoadStep(concurrency=225, duration_sec=60, ramp_delay=0.010, warmup_sec=10),
            LoadStep(concurrency=250, duration_sec=60, ramp_delay=0.010, warmup_sec=10),
            LoadStep(concurrency=275, duration_sec=60, ramp_delay=0.008, warmup_sec=10),
            LoadStep(concurrency=300, duration_sec=60, ramp_delay=0.008, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Scratch push higher — VAD disabled, c=125/150/175/200 sweep.
    "scratch_125200": LoadProfile(
        name="scratch_125200",
        steps=[
            LoadStep(concurrency=125, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
            LoadStep(concurrency=150, duration_sec=60, ramp_delay=0.020, warmup_sec=10),
            LoadStep(concurrency=175, duration_sec=60, ramp_delay=0.015, warmup_sec=10),
            LoadStep(concurrency=200, duration_sec=60, ramp_delay=0.012, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Scratch experiment continuation — VAD disabled, c=60/70/80/90/100 sweep.
    "scratch_60100": LoadProfile(
        name="scratch_60100",
        steps=[
            LoadStep(concurrency=60,  duration_sec=60, ramp_delay=0.05,  warmup_sec=10),
            LoadStep(concurrency=70,  duration_sec=60, ramp_delay=0.04,  warmup_sec=10),
            LoadStep(concurrency=80,  duration_sec=60, ramp_delay=0.04,  warmup_sec=10),
            LoadStep(concurrency=90,  duration_sec=60, ramp_delay=0.035, warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03,  warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Scratch experiment — VAD disabled, c=10/20/30/40/50 sweep.
    "scratch_1050": LoadProfile(
        name="scratch_1050",
        steps=[
            LoadStep(concurrency=10, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.08, warmup_sec=10),
            LoadStep(concurrency=30, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
            LoadStep(concurrency=40, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    "direct_c50": LoadProfile(
        name="direct_c50",
        steps=[LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10)],
        fresh_container_per_step=True,
    ),
    "direct_c80": LoadProfile(
        name="direct_c80",
        steps=[LoadStep(concurrency=80, duration_sec=60, ramp_delay=0.04, warmup_sec=10)],
        fresh_container_per_step=True,
    ),
    # Single-step profiles for reproducibility checks at each impl's best row.
    "direct_c20": LoadProfile(
        name="direct_c20",
        steps=[
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    "at_py_c80": LoadProfile(
        name="at_py_c80",
        steps=[
            LoadStep(concurrency=80, duration_sec=60, ramp_delay=0.04, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Single-step profile for the VAD pool-size matrix at fixed c=100.
    "at_rust_c100": LoadProfile(
        name="at_rust_c100",
        steps=[
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # VAD_POOL_SIZE scaling test — does raising the pool unstick the c=100 ceiling?
    "at_rust_pool_100140": LoadProfile(
        name="at_rust_pool_100140",
        steps=[
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
            LoadStep(concurrency=120, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
            LoadStep(concurrency=140, duration_sec=60, ramp_delay=0.02, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    "at_rust_120160": LoadProfile(
        name="at_rust_120160",
        steps=[
            LoadStep(concurrency=120, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
            LoadStep(concurrency=140, duration_sec=60, ramp_delay=0.02,  warmup_sec=10),
            LoadStep(concurrency=160, duration_sec=60, ramp_delay=0.018, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Direct-pipecat + uvloop probe — push past c=20 to see if uvloop lifts the cliff.
    "direct_2040": LoadProfile(
        name="direct_2040",
        steps=[
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=30, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
            LoadStep(concurrency=40, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # uvloop A/B — AT + Python VAD at best=60 plus two more steps past it.
    "at_py_6080": LoadProfile(
        name="at_py_6080",
        steps=[
            LoadStep(concurrency=60, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=70, duration_sec=60, ramp_delay=0.04, warmup_sec=10),
            LoadStep(concurrency=80, duration_sec=60, ramp_delay=0.04, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # uvloop A/B — AT + Rust VAD at best=80 plus two more steps past it.
    "at_rust_80100": LoadProfile(
        name="at_rust_80100",
        steps=[
            LoadStep(concurrency=80,  duration_sec=60, ramp_delay=0.04, warmup_sec=10),
            LoadStep(concurrency=90,  duration_sec=60, ramp_delay=0.035, warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # AT + Rust VAD cliff hunt — push past where PyVAD plateaued.
    # Linear CPU extrapolation suggests cliff somewhere around c=140-160.
    "at_rust_cliff": LoadProfile(
        name="at_rust_cliff",
        steps=[
            LoadStep(concurrency=80,  duration_sec=60, ramp_delay=0.04, warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
            LoadStep(concurrency=120, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
            LoadStep(concurrency=140, duration_sec=60, ramp_delay=0.02, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # 2-step profile for re-running c=50/60 with raised PHRASE_GAP_THRESHOLD
    # to see the true silence tail past the 80 ms metric cap.
    "at_5060": LoadProfile(
        name="at_5060",
        steps=[
            LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=60, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # AT + Rust VAD probe at c=40/50/60 (no c=70 to keep runtime tight).
    "at_rust_4060": LoadProfile(
        name="at_rust_4060",
        steps=[
            LoadStep(concurrency=40, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
            LoadStep(concurrency=60, duration_sec=60, ramp_delay=0.05, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # 2-step profile used to smoke-test fresh_container_per_step
    "lk_horiz": LoadProfile(
        name="lk_horiz",
        steps=[
            LoadStep(concurrency=50,  duration_sec=60, ramp_delay=0.05,  warmup_sec=10),
            LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03,  warmup_sec=10),
            LoadStep(concurrency=150, duration_sec=60, ramp_delay=0.02,  warmup_sec=10),
            LoadStep(concurrency=200, duration_sec=60, ramp_delay=0.012, warmup_sec=10),
            LoadStep(concurrency=250, duration_sec=60, ramp_delay=0.010, warmup_sec=10),
        ],
        fresh_container_per_step=False,
    ),
    "lk_horiz_160190": LoadProfile(
        name="lk_horiz_160190",
        steps=[
            LoadStep(concurrency=160, duration_sec=60, ramp_delay=0.018, warmup_sec=10),
            LoadStep(concurrency=170, duration_sec=60, ramp_delay=0.016, warmup_sec=10),
            LoadStep(concurrency=180, duration_sec=60, ramp_delay=0.015, warmup_sec=10),
            LoadStep(concurrency=190, duration_sec=60, ramp_delay=0.013, warmup_sec=10),
        ],
        fresh_container_per_step=False,
    ),
    "lk_horiz_150250": LoadProfile(
        name="lk_horiz_150250",
        steps=[
            LoadStep(concurrency=150, duration_sec=60, ramp_delay=0.02,  warmup_sec=10),
            LoadStep(concurrency=200, duration_sec=60, ramp_delay=0.012, warmup_sec=10),
            LoadStep(concurrency=250, duration_sec=60, ramp_delay=0.010, warmup_sec=10),
        ],
        fresh_container_per_step=False,
    ),
    "lk_105120": LoadProfile(
        name="lk_105120",
        steps=[
            LoadStep(concurrency=105, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
            LoadStep(concurrency=110, duration_sec=60, ramp_delay=0.03, warmup_sec=10),
            LoadStep(concurrency=115, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
            LoadStep(concurrency=120, duration_sec=60, ramp_delay=0.025, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Single-step confirmation profiles for variance checks against the
    # headline-table numbers.
    "direct_c50_var": LoadProfile(
        name="direct_c50_var",
        steps=[LoadStep(concurrency=50, duration_sec=60, ramp_delay=0.05, warmup_sec=10)],
        fresh_container_per_step=True,
    ),
    "at_horiz_c200": LoadProfile(
        name="at_horiz_c200",
        steps=[LoadStep(concurrency=200, duration_sec=60, ramp_delay=0.012, warmup_sec=10)],
        fresh_container_per_step=False,
    ),
    "at_horiz_c100": LoadProfile(name="at_horiz_c100", steps=[LoadStep(concurrency=100, duration_sec=60, ramp_delay=0.03, warmup_sec=10)]),
    "at_horiz_c110": LoadProfile(name="at_horiz_c110", steps=[LoadStep(concurrency=110, duration_sec=60, ramp_delay=0.028, warmup_sec=10)]),
    "at_horiz_c115": LoadProfile(name="at_horiz_c115", steps=[LoadStep(concurrency=115, duration_sec=60, ramp_delay=0.026, warmup_sec=10)]),
    "at_horiz_c120": LoadProfile(name="at_horiz_c120", steps=[LoadStep(concurrency=120, duration_sec=60, ramp_delay=0.025, warmup_sec=10)]),
    "at_horiz_c125": LoadProfile(name="at_horiz_c125", steps=[LoadStep(concurrency=125, duration_sec=60, ramp_delay=0.025, warmup_sec=10)]),
    "at_horiz_c150": LoadProfile(name="at_horiz_c150", steps=[LoadStep(concurrency=150, duration_sec=60, ramp_delay=0.02, warmup_sec=10)]),
    "at_horiz_c175": LoadProfile(name="at_horiz_c175", steps=[LoadStep(concurrency=175, duration_sec=60, ramp_delay=0.015, warmup_sec=10)]),
    "lk_horiz_c140": LoadProfile(name="lk_horiz_c140", steps=[LoadStep(concurrency=140, duration_sec=60, ramp_delay=0.021, warmup_sec=10)]),
    "lk_horiz_c150": LoadProfile(
        name="lk_horiz_c150",
        steps=[LoadStep(concurrency=150, duration_sec=60, ramp_delay=0.02, warmup_sec=10)],
        fresh_container_per_step=False,
    ),
    "lk_c27": LoadProfile(
        name="lk_c27",
        steps=[LoadStep(concurrency=27, duration_sec=60, ramp_delay=0.07, warmup_sec=10)],
        fresh_container_per_step=True,
    ),
    # Probe the c=20 → c=30 cliff seen on LiveKit vertical 4-CPU.
    "lk_2028": LoadProfile(
        name="lk_2028",
        steps=[
            LoadStep(concurrency=22, duration_sec=60, ramp_delay=0.08, warmup_sec=10),
            LoadStep(concurrency=24, duration_sec=60, ramp_delay=0.08, warmup_sec=10),
            LoadStep(concurrency=26, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
            LoadStep(concurrency=28, duration_sec=60, ramp_delay=0.07, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    "fresh_smoke": LoadProfile(
        name="fresh_smoke",
        steps=[
            LoadStep(concurrency=5, duration_sec=10, ramp_delay=0.1),
            LoadStep(concurrency=10, duration_sec=10, ramp_delay=0.1),
        ],
        fresh_container_per_step=True,
    ),
    # Per-vCPU sweep: single instance @ 1 CPU / 4 GB RAM, VAD off.
    # Concurrency stepped 2..20 in 2s — fine-grained probe to find the per-vCPU
    # ceiling for direct-pipecat, AT+pipecat, and AT+LiveKit individually.
    "per_vcpu_2_20": LoadProfile(
        name="per_vcpu_2_20",
        steps=[
            LoadStep(concurrency=2,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=4,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=6,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=8,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=10, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=12, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=14, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=16, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=18, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
        ],
        fresh_container_per_step=True,
    ),
    # Same sweep, but no fresh-container restarts. Faster for runs where
    # we expect the server to be saturated regardless of warm/cold state.
    "per_vcpu_2_20_warm": LoadProfile(
        name="per_vcpu_2_20_warm",
        steps=[
            LoadStep(concurrency=2,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=4,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=6,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=8,  duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=10, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=12, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=14, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=16, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=18, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
            LoadStep(concurrency=20, duration_sec=60, ramp_delay=0.10, warmup_sec=10),
        ],
        fresh_container_per_step=False,
    ),
}
