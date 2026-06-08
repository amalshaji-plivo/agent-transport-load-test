#!/bin/bash
# Python pipecat UP-sweep under the accepted 6.5ms silence bar: find the true
# ceiling (where delivery <100% or CPU >80% of 400% cap). One container per test
# (spin up / run / spin down), 20ms frames (AUDIO_OUT_10MS_CHUNKS=2), W=4, 4 vCPU.
# Reports instance concurrency AND per-core equivalent (c/4).
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
export CPU_LIMIT=4.0 MEM_LIMIT=8G WORKERS=4 AUDIO_OUT_10MS_CHUNKS=2 DIRECT_ENABLE_VAD=true
SVC=direct-pipecat; C=agent-transport-load-test-${SVC}-1
RES=$HOME/bench-out/py-upsweep; mkdir -p "$RES"; PROG=$RES/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
log "###### Python pipecat UP-sweep (20ms frames, W=4, 4 vCPU) ######"
for STEP in c30 c40 c50 c60; do
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait $SVC mock-services >>"$PROG" 2>&1; sleep 8
  docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile "$STEP" --target direct --no-docker \
      --direct-url ws://localhost:8080 --docker-container "$C" --output "/work/_up_${STEP}.json" \
    >>"$RES/$STEP.out" 2>&1
  [ -s "$WORK_DIR/_up_${STEP}.json" ] && mv "$WORK_DIR/_up_${STEP}.json" "$RES/$STEP.json"
  python3 -c "
import json
s=json.load(open('$RES/$STEP.json'))['summaries'][0];r=s['resources']
o,t=s['sessions_with_output'],s['total_sessions']
cpu=r['mean_cpu']; sil=s['within_phrase_gap']['p90']*1000-20  # 20ms baseline (20ms frames)
sil=max(0,sil)
gate = (o==t) and (cpu<320) and (sil<=6.5)
print(f'  $STEP: {o}/{t} (per-core c{t//4}) | CPU {cpu:.0f}% of 400 ({cpu/4:.0f}% norm) | sil_p90 {sil:.1f}ms | GATE {\"PASS\" if gate else \"FAIL\"}')
" >>"$PROG" 2>>"$PROG" || log "  $STEP PARSE FAIL"
done
docker compose down -v >/dev/null 2>&1 || true
log "###### PY_UPSWEEP DONE ######"
