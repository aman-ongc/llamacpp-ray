# Distributed Enterprise LLM Platform

## ONGC Intranet AI Infrastructure

### Qwen 3.6 35B A3B + llama.cpp + Ray/Kubernetes + FastAPI

---

# 1. Project Overview

This project aims to build a distributed, enterprise-grade, self-hosted AI inference platform for ONGC intranet usage.

The platform will serve large language models internally across multiple GPU-enabled workstations while providing:

* Centralized API access
* Load balancing
* Distributed inference
* Autoscaling behavior
* Authentication
* User tracking
* Observability
* Multi-model extensibility
* Future AI platform capabilities

The platform must remain:

* Modular
* Extensible
* Infrastructure-flexible
* Replaceable
* Vendor-independent

This is not merely an inference server deployment.

This project forms the foundation for:

* Internal AI services
* Enterprise inference infrastructure
* Future multimodal systems
* RAG platforms
* Agentic AI systems
* Internal AI tooling
* Organization-wide LLM access

---

# 2. Current Context

## Existing State

One workstation already runs:

* Qwen 3.6 35B A3B GGUF
* llama.cpp
* Hybrid GPU + CPU inference

Current inference launch example:

```bash
./build/bin/llama-server   -m /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf   --mmproj /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf   -ngl 999   -c 65536   --host 10.208.211.62   --port 8080   --parallel 2   --no-context-shift   --flash-attn  on --cache-type-k q8_0   --cache-type-v q8_0   --cont-batching   --spec-type draft-mtp   --spec-draft-n-max 4
```

The next phase involves:

* Multiple workstations
* Shared inference access
* Distributed scheduling
* Centralized APIs
* Internal enterprise operations

---

# 3. Infrastructure Details:

A detailed file is attached at @docs/infra.md

## Environment Characteristics

Deployment environment:

* ONGC intranet only
* No internet exposure
* Internal firewall/proxy restrictions
* GPU workstations
* Internal DNS/networking
* Mixed hardware possible

Potential limitations:

* Corporate proxy interception
* Firewall restrictions
* Internal TLS requirements
* Restricted outbound internet

---

# 4. Important Networking Constraints

## Proxy Considerations

Internal traffic may require explicit proxy bypassing.

Example:

```bash
curl --noproxy "*" http://node-ip:8000/health
```

This indicates:

* Enterprise proxy inspection
* Internal firewall rules
* Possible proxy interference with:

  * HTTP streaming
  * gRPC
  * WebSockets
  * Ray internal RPC

---

# 5. Mandatory Network Configuration

## Global no_proxy Configuration

All containers and services should include:

```bash
export no_proxy="localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in"
```

Docker:

```dockerfile
ENV no_proxy=localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in
```

Ray-specific:

```bash
export RAY_grpc_enable_http_proxy=false
```

This is critical for:

* Ray cluster communication
* Streaming responses
* Internal node-to-node communication
* Health checks
* Metrics collection

---

# 6. Primary Goals

## Functional Goals

The platform should provide:

* Unified inference endpoint
* OpenAI-compatible APIs
* API key management
* User authentication
* Usage tracking
* Request logging
* Distributed inference
* Load balancing
* Intelligent routing
* Autoscaling behavior
* Streaming support
* Metrics dashboards
* Health monitoring
* Internal administrative APIs

---

# 7. Non-Functional Goals

The platform should prioritize:

* Reliability
* Scalability
* GPU efficiency
* Low latency
* Fault tolerance
* Modularity
* Observability
* Maintainability
* Security
* Future extensibility

---

# 8. Architectural Philosophy

The architecture should remain:

* Componentized
* Replaceable
* Infrastructure-flexible
* AI-workload aware

Avoid:

* Tight coupling
* Monolithic deployment patterns
* Vendor lock-in
* Hardcoded orchestration assumptions

Every major layer should remain independently replaceable.

---

# 9. High-Level Recommended Architecture

```text
                    Internal Users
                           |
                    Internal DNS
                           |
                    NGINX Proxy
                           |
                    FastAPI Gateway
                           |
                     Ray Head Node
                           |
    ---------------------------------------------------
    |                    |                           |
 GPU Workstation-1   GPU Workstation-2        GPU Workstation-3
 Ray Worker          Ray Worker               Ray Worker
 llama.cpp           llama.cpp                llama.cpp
 Qwen 35B            Qwen 35B                 Qwen 35B
```

---

# 10. Core Architectural Layers

The platform can be viewed as multiple independent layers:

| Layer           | Responsibility             |
| --------------- | -------------------------- |
| Reverse Proxy   | Routing, TLS, ACL          |
| API Gateway     | Auth, logging, routing     |
| Orchestration   | Scheduling and scaling     |
| Inference Layer | Model execution            |
| Observability   | Metrics and logs           |
| Storage         | User and usage persistence |
| Queueing        | Request coordination       |

---

# 11. Inference Engine Layer

## Primary Choice

* llama.cpp

Reasons:

* GGUF support
* CPU/GPU hybrid inference
* Lightweight deployment
* Good quantization support
* Open-source
* Stable inference server mode

---

# 12. Alternative Inference Engines

The architecture should remain compatible with future alternatives:

## Potential Future Engines (not for now)

* vLLM
* TensorRT-LLM
* SGLang
* LMDeploy
* TGI
* Ollama
* Aphrodite
* ExLlama

No architectural layer should tightly depend on llama.cpp specifics.

---

# 13. Orchestration Strategy

Two primary orchestration approaches are considered:

## Option A: Ray Serve (go for Option A for now)

## Option B: Kubernetes

---

# 14. Ray Serve Strategy

## Recommended Initial Path

Ray is highly suitable for AI-native workloads.

Advantages:

* GPU-aware scheduling
* Distributed execution
* Async-native
* Actor-based inference
* Easier autoscaling
* Easier workload orchestration
* Better AI workload semantics

---

# 15. Ray-Based Architecture

```text
                    Internal Users
                           |
                    FastAPI Gateway
                           |
                     Ray Head Node
                           |
        ------------------------------------------------
        |                     |                        |
    Ray Worker           Ray Worker              Ray Worker
     Node-1               Node-2                  Node-3
        |                     |                        |
   llama.cpp Actor      llama.cpp Actor       llama.cpp Actor
```

---

# 16. Ray Responsibilities

Ray should handle:

* Worker orchestration
* Distributed scheduling
* GPU-aware placement
* Replica scaling
* Health monitoring
* Queue handling
* Distributed execution

---

# 17. Kubernetes Strategy

Kubernetes remains a strong long-term option.

Advantages:

* Enterprise standardization
* Mature ecosystem
* Strong HA
* Strong networking
* Operational familiarity
* Better infrastructure lifecycle management

---

# 18. Kubernetes Limitations for AI

Kubernetes alone is not AI-native.

Limitations:

* Limited inference awareness
* Primitive GPU scheduling
* No native token-aware routing
* No inference-aware queueing
* More operational overhead

---

# 19. Long-Term Ideal Architecture

Potential future architecture:

```text
Kubernetes
    +
KubeRay
    +
GPU Operator
```

This combines:

* Enterprise infra orchestration
* AI-native scheduling

---

# 20. Recommended Development Progression

## Initial Recommendation

Do NOT start with full Kubernetes complexity.

Preferred starting stack:

```text
Docker
+
Ray Serve
+
FastAPI
```

Then evolve later if required.

---

# 21. Containerization Philosophy

Everything should run in containers.

Benefits:

* Portability
* Reproducibility
* Easier upgrades
* Easier rollback
* Infrastructure consistency

---

# 22. Worker Node Containers

Each worker node may contain:

```text
- llama.cpp server
- metrics exporter
- health monitor
- Ray worker
```

Potential future additions:

* embedding server
* reranker server
* vision model server

---

# 23. Gateway Node Containers

Gateway node may contain:

```text
- FastAPI gateway
- Redis
- PostgreSQL
- Prometheus
- Grafana
- NGINX
```

---

# 24. API Gateway Responsibilities

The FastAPI gateway becomes the platform brain.

Responsibilities:

* API key validation
* User authentication
* Request logging
* Usage tracking
* Request routing
* Queue management
* Rate limiting
* Streaming proxying
* Health checks
* Administrative APIs

---

# 25. API Design Philosophy

The platform should expose:

* OpenAI-compatible APIs
* Internal admin APIs
* Infrastructure APIs

This maximizes ecosystem compatibility.

---

# 26. OpenAI-Compatible APIs

Primary endpoints:

```text
/v1/chat/completions
/v1/completions
/v1/models
/v1/embeddings
```

This enables compatibility with:

* LangChain
* OpenWebUI
* CrewAI
* Continue.dev
* VSCode plugins
* Internal AI applications

---

# 27. Administrative APIs

Potential endpoints:

```text
/admin/users
/admin/keys
/admin/metrics
/admin/nodes
/admin/logs
/admin/models
```

---

# 28. Infrastructure APIs

```text
/health
/ready
/live
/metrics
```

---

# 29. Authentication Strategy

Recommended:

* API keys initially

Potential future:

* LDAP
* Active Directory
* OAuth2
* SSO integration

---

# 30. API Key Philosophy

Each user/application should receive:

* Unique API keys
* Usage quotas
* Audit trails
* Department association

Potential future:

* Tiered quotas
* Priority scheduling
* Team-based access

---

# 31. Logging Philosophy

Every request should log:

* User
* API key
* Timestamp
* Model
* Node selected
* Latency
* Prompt tokens
* Completion tokens
* Error codes
* Queue duration

---

# 32. Database Layer

## Primary Recommendation

* PostgreSQL

Stores:

* Users
* API keys
* Usage records
* Metrics
* Audit logs
* Request history

---

# 33. Queueing Layer

## Recommended

* Redis

Uses:

* Request queues
* Distributed locks
* Shared state
* Rate limit counters
* Node metadata

Potential future alternatives:

* Kafka
* RabbitMQ
* NATS

---

# 34. Load Balancing Philosophy

Routing decisions should consider:

* Queue depth
* Active requests
* GPU utilization
* VRAM availability
* Node health

---

# 35. Initial Routing Strategy

Simple strategies first:

* Least active requests
* Queue-length routing

Avoid early complexity.

---

# 36. Future Routing Possibilities

Potential advanced routing:

* Token/sec aware scheduling
* SLA-based routing
* User-priority routing
* Cost-aware scheduling
* Model-specialized routing

---

# 37. Autoscaling Philosophy

This is workstation-based infrastructure.

Autoscaling means:

* Activating more replicas
* Using more workstations
* Increasing worker pools

NOT cloud VM provisioning.

---

# 38. Scaling Signals

Potential scaling triggers:

* Queue depth
* Latency
* GPU utilization
* Request backlog
* Token throughput degradation

---

# 39. Concurrency Philosophy

For llama.cpp:

* Excessive concurrency often degrades performance

Prefer:

* Limited concurrent generations
* Centralized queueing

Avoid:

* KV cache thrashing
* VRAM fragmentation
* Context swapping overhead

---

# 40. Recommended Concurrency Strategy

Typical starting strategy:

* 1-3 active generations per GPU node

Tune empirically.

---

# 41. Security Philosophy

Because deployment is intranet-only:

Primary concerns:

* Internal misuse
* Unauthorized access
* Resource abuse
* Auditability

---

# 42. Recommended Security Controls

Recommended:

* API keys
* Internal TLS
* IP allowlists
* Admin endpoint protection
* Audit logging
* Internal ACLs

Potential future:

* RBAC
* LDAP/AD integration

---

# 43. Reverse Proxy Layer

## Primary Recommendation

* NGINX

Responsibilities:

* Reverse proxying
* TLS termination
* Streaming support
* ACLs
* Internal routing

Alternatives:

* Envoy
* HAProxy
* Traefik

---

# 44. Observability Philosophy

Observability is mandatory.

Without metrics:

* Scaling decisions fail
* Bottlenecks remain invisible
* GPU utilization becomes opaque

---

# 45. Metrics Stack

## Recommended

Metrics:

* Prometheus

Visualization:

* Grafana

Logs:

* Loki or ELK

---

# 46. Important Metrics

Track:

* GPU utilization
* VRAM usage
* Token/sec
* Queue depth
* Request latency
* Active requests
* Failed requests
* Node health

---

# 47. Dashboard Philosophy

Dashboards should support:

* Per-node metrics
* Per-user metrics
* Model usage statistics
* Latency trends
* Queue monitoring

---

# 48. Failure Handling Philosophy

The platform should tolerate:

* Node failures
* GPU failures
* Hung inference processes
* Queue spikes

Mechanisms:

* Health checks
* Request retries
* Node draining
* Graceful failover

---

# 49. Health Monitoring

Every worker should expose:

* health endpoint
* metrics endpoint

Potential checks:

* model loaded
* GPU reachable
* inference responsiveness

---

# 50. Streaming Support

Streaming should remain first-class.

The platform should support:

* SSE streaming
* OpenAI-compatible streaming
* Long-running responses

Proxy layers must support streaming correctly.

---

# 51. Future Multi-Model Support

Architecture should support:

* Multiple LLMs
* Embedding models
* Vision models
* Audio models
* Rerankers

Avoid single-model assumptions.

---

# 52. Future AI Platform Expansion

Potential future capabilities:

* RAG pipelines
* MCP servers
* Agentic workflows
* Multi-agent systems
* Internal copilots
* Batch inference
* Fine-tuned model hosting

---

# 53. Future Integration Possibilities

Potential ecosystem integrations:

* OpenWebUI
* LangChain
* CrewAI
* Continue.dev
* Internal ONGC tools
* VSCode assistants

Because APIs remain OpenAI-compatible.

---

# 54. Recommended Execution Phases

## Phase 1

Core distributed inference

Components:

* Docker
* llama.cpp
* Ray
* FastAPI
* PostgreSQL

Goal:

* Stable distributed inference

---

## Phase 2

Observability and scaling

Add:

* Prometheus
* Grafana
* Redis

Goal:

* Visibility and operational maturity

---

# 55. Phase 3

Enterprise hardening

Add:

* NGINX
* TLS
* RBAC
* LDAP integration
* Audit dashboards

Goal:

* Enterprise readiness

---

# 56. Phase 4

Advanced orchestration

Potential additions:

* Kubernetes
* KubeRay
* Multi-model scheduling
* Advanced routing

Goal:

* Enterprise AI platform maturity

---

# 57. Engineering Principles

The project should prioritize:

* Simplicity first
* Incremental scaling
* Replaceable components
* Observable systems
* Measurable bottlenecks
* Operational clarity

Avoid:

* Premature microservices
* Overengineering
* Unnecessary abstraction
* Early Kubernetes complexity

---

# 58. Long-Term Vision

This platform should evolve into:

* Internal enterprise AI infrastructure
* Centralized model serving platform
* Multi-team AI backbone
* Future multimodal AI system

The architecture must therefore remain:

* Extensible
* Observable
* Infrastructure-flexible
* AI-workload aware
* Enterprise-ready
