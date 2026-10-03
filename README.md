# Simple Chat API

```bash
pip install -r requirements.txt
uvicorn app:app --reload      # open http://127.0.0.1:8000/docs
pytest -v                     # run tests
```

In /docs: register → login → copy `access_token` → click **Authorize** and paste it.

Stream from a terminal:
```bash
curl -N -X POST http://127.0.0.1:8000/conversations/1/stream \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"content":"hello"}'
```
