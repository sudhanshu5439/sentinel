# Sentinel: the front door. Run `make help` to see every target.
SHELL := /bin/bash
.DEFAULT_GOAL := help

CLUSTER   ?= sentinel
NAMESPACE ?= sentinel
SERVICES  := order-api risk-engine notifier
TAG       ?= dev
TF_LOCAL  := infra/terraform/envs/local-kind
SECRETS   := deploy/local/secrets.env

##@ Everyday

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage: make \033[36m<target>\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

.PHONY: up
up: cluster images load deploy wait ## Create the cluster, build + load images, deploy everything
	@echo -e "\n\033[32mSentinel is up.\033[0m Try: make smoke"

.PHONY: down
down: ## Delete the local cluster (frees the RAM)
	terraform -chdir=$(TF_LOCAL) destroy -auto-approve

.PHONY: status
status: ## Show nodes, pods and services
	@kubectl get nodes -o wide
	@echo; kubectl -n $(NAMESPACE) get pods,svc -o wide

.PHONY: smoke
smoke: ## Place test orders end to end and check the notifier received them
	@bash scripts/smoke.sh

.PHONY: reload
reload: images load ## Rebuild images, load them and restart the apps (after code changes)
	kubectl -n $(NAMESPACE) rollout restart deploy/order-api deploy/risk-engine deploy/notifier
	$(MAKE) wait

.PHONY: logs
logs: ## Tail logs of one service: make logs SVC=order-api
	kubectl -n $(NAMESPACE) logs -f deploy/$(SVC) --tail=50

##@ Building blocks

.PHONY: cluster
cluster: ## Create the kind cluster with Terraform (1 control plane + 2 workers)
	terraform -chdir=$(TF_LOCAL) init -input=false -upgrade >/dev/null
	terraform -chdir=$(TF_LOCAL) apply -input=false -auto-approve
	kind export kubeconfig --name $(CLUSTER)

.PHONY: images
images: ## Build the three service images
	@for s in $(SERVICES); do \
	  echo "==> building sentinel-$$s:$(TAG)"; \
	  docker build -q -t sentinel-$$s:$(TAG) apps/$$s || exit 1; \
	done

.PHONY: load
load: ## Load the images into the kind nodes (no registry needed locally)
	@for s in $(SERVICES); do kind load docker-image sentinel-$$s:$(TAG) --name $(CLUSTER); done

$(SECRETS):
	@umask 077; echo "POSTGRES_PASSWORD=$$(openssl rand -hex 16)" > $(SECRETS)
	@echo "generated $(SECRETS) (git-ignored)"

.PHONY: secrets
secrets: $(SECRETS) ## Generate local secrets once (never committed)

.PHONY: deploy
deploy: secrets ## Apply the Kubernetes manifests
	kubectl apply -k deploy/local

.PHONY: wait
wait: ## Wait until every workload is ready
	kubectl -n $(NAMESPACE) rollout status statefulset/postgres --timeout=180s
	kubectl -n $(NAMESPACE) rollout status deploy/redis --timeout=120s
	@for s in $(SERVICES); do kubectl -n $(NAMESPACE) rollout status deploy/$$s --timeout=180s || exit 1; done

##@ Supply chain

OWNER ?= sudhanshu5439
.PHONY: verify
verify: ## Verify a published image's signature: make verify SVC=order-api
	cosign verify ghcr.io/$(OWNER)/sentinel-$(SVC):main \
	  --certificate-identity-regexp 'https://github.com/$(OWNER)/sentinel/.github/workflows/supply-chain.yml@.*' \
	  --certificate-oidc-issuer https://token.actions.githubusercontent.com | jq '.[0].optional | {Issuer, Subject}'
	gh attestation verify oci://ghcr.io/$(OWNER)/sentinel-$(SVC):main --owner $(OWNER)

##@ Quality

.PHONY: test
test: ## Run every service's unit tests
	cd apps/order-api && uv run pytest -q
	cd apps/notifier && uv run pytest -q
	cd apps/risk-engine && go test -race -count=1 ./...

.PHONY: lint
lint: ## Lint and format-check all code
	cd apps/order-api && uv run ruff check . && uv run ruff format --check .
	cd apps/notifier && uv run ruff check . && uv run ruff format --check .
	cd apps/risk-engine && test -z "$$(gofmt -l .)" && go vet ./...
