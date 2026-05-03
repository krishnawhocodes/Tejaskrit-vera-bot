# Minimal curl self-test

Run bot first:

```bash
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Health:

```bash
curl http://localhost:8080/v1/healthz
```

Metadata:

```bash
curl http://localhost:8080/v1/metadata
```

Push a tiny category, merchant, and trigger:

```bash
curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{"scope":"category","context_id":"dentists","version":1,"delivered_at":"2026-04-26T10:00:00Z","payload":{"slug":"dentists","display_name":"Dentists","peer_stats":{"avg_ctr":0.030},"digest":[{"id":"d1","title":"3-month fluoride recall cuts caries recurrence 38% better","source":"JIDA Oct 2026, p.14","trial_n":2100,"patient_segment":"high_risk_adults","summary":"38% lower recurrence in high-risk adults."}]}}'

curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{"scope":"merchant","context_id":"m1","version":1,"payload":{"merchant_id":"m1","category_slug":"dentists","identity":{"name":"Dr. Meera Dental Clinic","owner_first_name":"Meera","city":"Delhi","locality":"Lajpat Nagar","languages":["en","hi"]},"performance":{"views":2410,"calls":18,"directions":45,"ctr":0.021},"offers":[{"title":"Dental Cleaning @ ₹299","status":"active"}],"customer_aggregate":{"high_risk_adult_count":124},"signals":["ctr_below_peer_median"]}}'

curl -X POST http://localhost:8080/v1/context \
  -H "Content-Type: application/json" \
  -d '{"scope":"trigger","context_id":"t1","version":1,"payload":{"id":"t1","scope":"merchant","kind":"research_digest","source":"external","merchant_id":"m1","customer_id":null,"payload":{"category":"dentists","top_item_id":"d1"},"urgency":2,"suppression_key":"research:test","expires_at":"2026-12-31T00:00:00Z"}}'
```

Tick:

```bash
curl -X POST http://localhost:8080/v1/tick \
  -H "Content-Type: application/json" \
  -d '{"now":"2026-04-26T10:35:00Z","available_triggers":["t1"]}'
```

Reply intent test:

```bash
curl -X POST http://localhost:8080/v1/reply \
  -H "Content-Type: application/json" \
  -d '{"conversation_id":"conv_m1_t1","merchant_id":"m1","from_role":"merchant","message":"Ok lets do it. Whats next?","turn_number":2}'
```
