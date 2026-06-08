#!/bin/bash
# QoE-gated 4-concurrent sweep: find the per-container concurrency where 4 agents
# running together still pass ALL gates jointly: delivery 100% AND CPU/cont <80%
# AND aggregate silence p90 <=5ms. Sweep c DOWN from the isolated ceiling.
#   AT+pipecat : host net, ports 8101-8104, c in {12,10,8,6}
#   AT+livekit : bridge net (port-collision fix), ports 8111-8114, c in {10,8,6,4}
set -uo pipefail
WORK_DIR=/root/agent-stack-workspace/agent-transport-load-test; cd "$WORK_DIR"
RES=$HOME/bench-out/qoe-4c; mkdir -p "$RES"; PROG=$RES/progress.log; : > "$PROG"
log(){ echo "$(date '+%H:%M:%S') $*" | tee -a "$PROG"; }
IMG_PV=agent-transport-load-test-agent-transport-python-vad
IMG_LK=agent-transport-load-test-agent-transport-livekit
IMG_MOCK=agent-transport-load-test/mock-services:latest
PV_VAD='-e ENABLE_VAD=true -e VAD_BACKEND=python -e VAD_POOL_SIZE=8'
LK_ENV='-e ENABLE_TURN_DETECTOR=true -e ENABLE_VAD=false -e ENABLE_PY_VAD=false -e MIN_ENDPOINTING_DELAY=0.4 -e MAX_ENDPOINTING_DELAY=1.5'
wait_port(){ for _ in $(seq 1 120); do (echo >/dev/tcp/localhost/$1) 2>/dev/null && return 0; sleep 1; done; return 1; }
cleanup(){ docker rm -f mock4 a1 a2 a3 a4 2>/dev/null >/dev/null; docker network rm qoenet 2>/dev/null >/dev/null||true; }

aggregate(){ # arm c  -> reads $RES/${arm}-c${c}_a{1..4}.json
  python3 - "$RES" "$1" "$2" <<'PY'
import json,sys,statistics as st
res,arm,c=sys.argv[1],sys.argv[2],int(sys.argv[3])
to=t=0;cpus=[];sils=[];withn=0
for k in range(1,5):
    try:s=json.load(open(f"{res}/{arm}-c{c}_a{k}.json"))['summaries'][0]
    except:continue
    to+=s['sessions_with_output'];t+=s['total_sessions']
    cpus.append(s['resources']['mean_cpu'])
    if s['within_phrase_gap'].get('count',0)>0: sils.append(s['audible_silence_gap']['p90']*1000); withn+=1
maxcpu=max(cpus) if cpus else 0
silp=max(sils) if sils else float('nan')
gate = (to==t) and (maxcpu<80) and (sils and silp<=5)
print(f"  4x c{c}: agg {to}/{t} | maxCPU/cont {maxcpu:.0f}% | sil_p90 {('%.1f'%silp) if sils else 'n/a'}ms ({withn}/4 measurable) | GATE {'PASS' if gate else 'FAIL'}")
PY
}

run_pipecat(){ local c=$1; cleanup
  docker run -d --name mock4 --network host $IMG_MOCK >>"$PROG" 2>&1; wait_port 9000||return
  for k in 1 2 3 4; do p=$((8100+k)); docker run -d --name a$k --network host --cpus=1.0 --memory=2g \
    -e MOCK_SERVICES_HOST=localhost:9000 -e WS_PORT=$p $PV_VAD $IMG_PV >>"$PROG" 2>&1; done
  for k in 1 2 3 4; do wait_port $((8100+k))||true; done; sleep 10
  for k in 1 2 3 4; do p=$((8100+k)); docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile c$c --target agent-transport-python-vad --no-docker \
      --at-python-url ws://localhost:$p --docker-container a$k --output "/work/_q_pipecat-c${c}_a${k}.json" \
      >>"$RES/pipecat-c${c}_a${k}.out" 2>&1 & done
  wait; for k in 1 2 3 4; do mv "$WORK_DIR/_q_pipecat-c${c}_a${k}.json" "$RES/pipecat-c${c}_a${k}.json" 2>/dev/null; done
  aggregate pipecat $c; cleanup
}

run_livekit(){ local c=$1; cleanup; docker network create qoenet >/dev/null
  docker run -d --name mock4 --network qoenet --network-alias mock-services -p 9000:9000 $IMG_MOCK >>"$PROG" 2>&1; wait_port 9000||return
  for k in 1 2 3 4; do p=$((8110+k)); docker run -d --name a$k --network qoenet --cpus=1.0 --memory=2g \
    -p $p:8083 -e MOCK_SERVICES_HOST=mock-services:9000 -e WS_PORT=8083 -e HTTP_PORT=8184 $LK_ENV $IMG_LK >>"$PROG" 2>&1
    wait_port $p||true; sleep 5; done
  sleep 10
  for k in 1 2 3 4; do p=$((8110+k)); docker run --rm --network host -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$WORK_DIR":/work -w /work -e TURN_SILENCE_MS=4000 bench-client:latest \
    python -m load_test.cli --profile c$c --target agent-transport-livekit --no-docker \
      --at-livekit-url ws://localhost:$p --docker-container a$k --output "/work/_q_livekit-c${c}_a${k}.json" \
      >>"$RES/livekit-c${c}_a${k}.out" 2>&1 & done
  wait; for k in 1 2 3 4; do mv "$WORK_DIR/_q_livekit-c${c}_a${k}.json" "$RES/livekit-c${c}_a${k}.json" 2>/dev/null; done
  aggregate livekit $c; cleanup
}

log "###### QoE-gated 4-concurrent sweep ######"
log "## AT+pipecat (4x, host net) ##"
for c in 12 10 8 6; do log "-- pipecat 4x c$c --"; run_pipecat $c | tee -a "$PROG"; done
log "## AT+livekit (4x, bridge net) ##"
for c in 10 8 6 4; do log "-- livekit 4x c$c --"; run_livekit $c | tee -a "$PROG"; done
log "###### QOE_4C_SWEEP DONE ######"
