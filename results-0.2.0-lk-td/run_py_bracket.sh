#!/bin/bash
# Bracket Python's ceiling: c40 x5 and c50 x5, one container per test (spin up/run/
# spin down), 20ms frames, W=4, 4 vCPU. Tests whether the up-sweep delivery dips
# (31/40, 35/50 at low CPU) are reproducible capacity limits or transient cadence
# artifacts, and gives the 5x treatment at the silence-bound candidate (c50).
# Gate: delivery 100% AND CPU<320% (80% of 400) AND silence p90 <=6.5ms.
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
export COMPOSE_PROJECT_NAME=agent-transport-load-test
export COMPOSE_FILE=$WORK_DIR/docker-compose.yml:$WORK_DIR/docker-compose.mocks.yml
export CPU_LIMIT=4.0 MEM_LIMIT=8G WORKERS=4 AUDIO_OUT_10MS_CHUNKS=2 DIRECT_ENABLE_VAD=true
SVC=direct-pipecat; C=agent-transport-load-test-${SVC}-1
RES=$HOME/bench-out/py-bracket; mkdir -p "$RES"; PROG=$RES/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
run(){ local STEP=$1 i=$2 tag=${1}-run${2}
  docker compose down -v >/dev/null 2>&1 || true
  docker compose up -d --force-recreate --wait $SVC mock-services >>"$PROG" 2>&1; sleep 8
  docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile "$STEP" --target direct --no-docker \
      --direct-url ws://localhost:8080 --docker-container "$C" --output "/work/_b_${tag}.json" \
    >>"$RES/$tag.out" 2>&1
  [ -s "$WORK_DIR/_b_${tag}.json" ] && mv "$WORK_DIR/_b_${tag}.json" "$RES/$tag.json"
  python3 -c "
import json
s=json.load(open('$RES/$tag.json'))['summaries'][0];r=s['resources']
o,t=s['sessions_with_output'],s['total_sessions']
cpu=r['mean_cpu']; sil=max(0,s['within_phrase_gap']['p90']*1000-20)
print(f'  $tag: {o}/{t} | CPU {cpu:.0f}% | sil_p90 {sil:.1f}ms | {\"PASS\" if (o==t and cpu<320 and sil<=6.5) else \"FAIL\"}')
" >>"$PROG" 2>>"$PROG" || log "  $tag PARSE FAIL"
}
log "###### Python bracket: c40 x5, c50 x5 (20ms frames, W=4) ######"
log "## c40 x5 ##"; for i in 1 2 3 4 5; do run c40 $i; done
log "## c50 x5 ##"; for i in 1 2 3 4 5; do run c50 $i; done
docker compose down -v >/dev/null 2>&1 || true
log "###### PY_BRACKET DONE ######"
