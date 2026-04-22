.PHONY: api web install infra.up infra.down infra.logs db.migrate

install:
	cd apps/api && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd apps/web && npm install

api:
	cd apps/api && .venv/bin/uvicorn main:app --reload --port 8000

web:
	cd apps/web && npm run dev

infra.up:
	docker compose -f infra/docker-compose.yml up -d

infra.down:
	docker compose -f infra/docker-compose.yml down

infra.logs:
	docker compose -f infra/docker-compose.yml logs -f

db.migrate:
	cd apps/api && .venv/bin/alembic upgrade head
