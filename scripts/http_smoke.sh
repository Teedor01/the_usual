set -u
PORT=${PORT:-8077}; B=http://127.0.0.1:$PORT; DATA=$(mktemp -d)
USUAL_DATA_DIR=$DATA python3 -m uvicorn app.api:create_app --factory --port $PORT --log-level warning > "$DATA/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
i=0; until curl -s -o /dev/null $B/api/info; do i=$((i+1)); [ $i -gt 40 ] && { echo "server did not start"; cat "$DATA/server.log"; exit 1; }; sleep 0.25; done
j() { python3 -c "import sys,json; v=json.load(sys.stdin); $1"; }
H='content-type: application/json'
echo "info:      $(curl -s $B/api/info)"
echo "index:     $(curl -s -o /dev/null -w '%{http_code} %{content_type}' $B/)"
SID=$(curl -s -X POST $B/api/sessions | j "print(v['session_id'])")
R=$(curl -s -X POST $B/api/sessions/$SID/instructions -H "$H" -d '{"text":"Pay Emeka the balance for those chairs.","idempotency_key":"curl-1"}')
echo "stage:     $(echo "$R" | j "d=v['draft']; print(v['outcome'], d['amount_display'], 'to', d['recipient_name'], '|', d['source'])")"
AID=$(echo "$R" | j "print(v['draft']['action_id'])"); HASH=$(echo "$R" | j "print(v['draft']['payload_hash'])")
echo "commit c1: $(curl -s -X POST $B/api/sessions/$SID/actions/$AID/commit -H "$H" -d "{\"commit_key\":\"c1\",\"confirmed_payload_hash\":\"$HASH\"}" | j "print(v['status'], v['amount_display'], 'simulated=%s' % v['simulated'])")"
echo "replay c1: $(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/sessions/$SID/actions/$AID/commit -H "$H" -d "{\"commit_key\":\"c1\",\"confirmed_payload_hash\":\"$HASH\"}")  (expect 200, stored response)"
echo "new key c2: $(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/sessions/$SID/actions/$AID/commit -H "$H" -d "{\"commit_key\":\"c2\",\"confirmed_payload_hash\":\"$HASH\"}")  (expect 409 already committed)"
echo "extra field: $(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/sessions/$SID/actions/$AID/commit -H "$H" -d "{\"commit_key\":\"c3\",\"confirmed_payload_hash\":\"$HASH\",\"amount_kobo\":1}")  (expect 422)"
echo "repeat:    $(curl -s -X POST $B/api/sessions/$SID/instructions -H "$H" -d '{"text":"Pay Emeka the balance for those chairs.","idempotency_key":"curl-2"}' | j "print(v['outcome'], v['reason'])")  (expect ASK nothing_owed)"
echo "ledger:    $(curl -s $B/api/sessions/$SID/ledger | j "p=[x for x in v['people'] if x['id']=='EO'][0]; print(p['name'], p['agreements'][0]['description'], 'outstanding', p['agreements'][0]['outstanding_kobo'], 'kobo')")"
ZERO=$(python3 -c "print('0'*32)")
echo "unknown:   $(curl -s -o /dev/null -w '%{http_code}' $B/api/sessions/$ZERO/ledger)  (expect 410)   malformed: $(curl -s -o /dev/null -w '%{http_code}' $B/api/sessions/nope/ledger)  (expect 400)"
echo "server errors in log: $(grep -c -i -E 'error|traceback' "$DATA/server.log")"
