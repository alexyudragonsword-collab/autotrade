.PHONY: dev test lint frontend build docker

dev:
	uvicorn app.main:app --app-dir backend --reload --host 0.0.0.0 --port 8000

test:
	pytest -q

lint:
	ruff check backend

frontend:
	cd frontend && npm install && npm run build

docker:
	docker compose -f docker/docker-compose.yml up --build
