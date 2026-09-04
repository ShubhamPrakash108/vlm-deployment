import logging
import sys

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("vlm-api")

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn, requests
import threading
from fastapi.security import APIKeyHeader
from fastapi.responses import StreamingResponse, JSONResponse
import httpx, json
from pydantic import BaseModel, HttpUrl
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import redis
import hashlib
import time
from fastapi.staticfiles import StaticFiles
import os
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import Counter, Histogram, Gauge
import subprocess
import psutil
import asyncio
from contextlib import asynccontextmanager
from PIL import Image
import base64
import io

# Prometheus Gauges 
azure_cpu      = Gauge("azure_cpu_percent",    "Azure VM CPU usage %")
azure_ram      = Gauge("azure_ram_percent",    "Azure VM RAM usage %")
azure_ram_used = Gauge("azure_ram_used_gb",    "Azure VM RAM used GB")
azure_disk     = Gauge("azure_disk_percent",   "Azure VM disk usage %")
azure_net_sent = Gauge("azure_net_sent_mb",    "Azure VM network bytes sent MB")
azure_net_recv = Gauge("azure_net_recv_mb",    "Azure VM network bytes received MB")

# Azure VM Metrics
def get_azure_vm_metrics():
    try:
        metrics = {
            "cpu_percent":       psutil.cpu_percent(interval=0.1),
            "ram_percent":       psutil.virtual_memory().percent,
            "ram_used_gb":       round(psutil.virtual_memory().used / (1024**3), 2),
            "disk_percent":      psutil.disk_usage("/").percent,
            "net_bytes_sent_mb": round(psutil.net_io_counters().bytes_sent / (1024**2), 2),
            "net_bytes_recv_mb": round(psutil.net_io_counters().bytes_recv / (1024**2), 2),
        }
        return metrics
    except Exception as e:
        logger.error("Failed to collect VM metrics: %s", e, exc_info=True)
        return None

async def get_azure_vm_metrics_async():
    return await asyncio.to_thread(get_azure_vm_metrics)

async def azure_metrics_background_loop():
    logger.info("Azure metrics background loop started (interval=15s)")
    while True:
        try:
            metrics = await get_azure_vm_metrics_async()
            if metrics:
                azure_cpu.set(metrics["cpu_percent"])
                azure_ram.set(metrics["ram_percent"])
                azure_ram_used.set(metrics["ram_used_gb"])
                azure_disk.set(metrics["disk_percent"])
                azure_net_sent.set(metrics["net_bytes_sent_mb"])
                azure_net_recv.set(metrics["net_bytes_recv_mb"])
            else:
                logger.warning("Skipping metrics update — collector returned None")
        except Exception as e:
            logger.error("Error in metrics background loop: %s", e, exc_info=True)
        await asyncio.sleep(15)

# Redis Connection
try:
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)
    r.ping()
    logger.info("Redis connected successfully on localhost:6379")
except redis.ConnectionError as e:
    logger.critical("Cannot connect to Redis on localhost:6379 — %s", e)
    logger.critical("The application will start but caching/rate-limiting will fail!")
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)
except Exception as e:
    logger.critical("Unexpected error connecting to Redis: %s", e, exc_info=True)
    r = redis.Redis(host='localhost', port=6379, decode_responses=True)

# Lifespan
http_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    limits = httpx.Limits(max_keepalive_connections=100, max_connections=500)
    http_client = httpx.AsyncClient(limits=limits, timeout=60.0)
    
    logger.info("Application startup — launching background tasks")
    task = asyncio.create_task(azure_metrics_background_loop())
    try:
        yield
    finally:
        logger.info("Application shutdown — cancelling background tasks")
        task.cancel()
        await http_client.aclose()
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Background metrics task cancelled cleanly")

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS middleware configured (allow_origins=*)")

# Prometheus Instrumentation
Instrumentator().instrument(app).expose(app)

inference_time = Histogram("inference_time_seconds", "Time taken for inference")
total_requests = Counter("total_requests_total", "Total API requests")
cache_hits     = Counter("cache_hits_total", "Cache hits")
cache_miss     = Counter("cache_miss_total", "Cache miss")
failed_requests = Counter("failed_requests_total", "Failed API requests")
logger.info("Prometheus metrics registered")

# Rate Limiter
def get_api_key(request: Request):
    return request.headers.get("X-API-Key")

limiter = Limiter(key_func=get_api_key, storage_uri="redis://localhost:6379")

def generate_cache_key(image_url, query):
    raw = image_url + query
    return hashlib.sha256(raw.encode()).hexdigest()

app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Auth 
API_KEY = "12345"
api_key_header = APIKeyHeader(name="X-API-Key")
VLLM_URL = ""

runpod_headers = {
    "Content-Type": "application/json",
    "Authorization": ""
}

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != API_KEY:
        logger.warning("Unauthorized access attempt with key: %s***", api_key[:3] if api_key else "None")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

# Request Model
class StreamVLMRequest(BaseModel):
    generation_id: str
    model: str
    image_url: HttpUrl
    query: str

# Image Validation
def _validate_image_bytes(image_bytes: bytes) -> dict:
    """Synchronous PIL processing — runs in a thread pool to avoid blocking the event loop."""
    img = Image.open(io.BytesIO(image_bytes))
    img.load()  # Force full decode so corrupt images fail here
    width, height = img.size
    return {"width": width, "height": height, "format": img.format}


async def validate_image_url(image_url: str):
    image_url = str(image_url)
    logger.info("Validating image URL: %s", image_url[:120])

    # Fetch the image
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
            response = await client.get(
                image_url,
                headers={"User-Agent": "Mozilla/5.0"}
            )
    except httpx.TimeoutException as e:
        logger.error("Timeout fetching image URL: %s — %s", image_url[:120], e)
        raise HTTPException(status_code=400, detail="Image fetch timed out")
    except httpx.ConnectError as e:
        logger.error("Connection error fetching image: %s — %s", image_url[:120], e)
        raise HTTPException(status_code=400, detail="Unable to fetch image — connection failed")
    except Exception as e:
        logger.error("Unexpected error fetching image: %s — %s", image_url[:120], e, exc_info=True)
        raise HTTPException(status_code=400, detail="Unable to fetch image")

    if response.status_code != 200:
        logger.warning("Image URL returned HTTP %d: %s", response.status_code, image_url[:120])
        raise HTTPException(status_code=400, detail=f"Unable to fetch image (HTTP {response.status_code})")

    content_type = response.headers.get("content-type", "")
    if "image" not in content_type:
        logger.warning("URL is not an image (content-type: %s): %s", content_type, image_url[:120])
        raise HTTPException(status_code=400, detail="URL is not an image")

    # Validate with PIL — offloaded to thread pool
    try:
        img_info = await asyncio.to_thread(_validate_image_bytes, response.content)
        width, height, fmt = img_info["width"], img_info["height"], img_info["format"]
        logger.info("Image validated: %dx%d, format=%s", width, height, fmt)
    except Exception as e:
        logger.error("PIL failed to open image: %s — %s", image_url[:120], e, exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid image format")

    if width < 256 or height < 256:
        logger.warning("Image too small: %dx%d — %s", width, height, image_url[:120])
        raise HTTPException(
            status_code=400,
            detail=f"Image too small ({width}x{height}), minimum 256x256"
        )

    if width > 1024 or height > 1024:
        logger.warning("Image too large: %dx%d — %s", width, height, image_url[:120])
        raise HTTPException(
            status_code=400,
            detail=f"Image too large ({width}x{height}), maximum 1024x1024"
        )

    return {"width": width, "height": height, "format": fmt}

# Stream Generator 
async def stream_generator(req):
    payload = {
        "model": req.model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": str(req.image_url)}},
            {"type": "text", "text": req.query}
        ]}],
        "stream": True
    }

    logger.info("Starting stream to vLLM — model=%s, generation_id=%s", req.model, req.generation_id)

    try:
        async with http_client.stream("POST", VLLM_URL, json=payload, headers=runpod_headers) as response:
            if response.status_code != 200:
                logger.error("vLLM returned HTTP %d during streaming", response.status_code)
                yield f"[ERROR] vLLM returned HTTP {response.status_code}"
                return

            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    logger.info("Stream completed for generation_id=%s", req.generation_id)
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except json.JSONDecodeError as e:
                    logger.warning("Malformed JSON chunk: %s — %s", data[:80], e)
                    continue
                except (KeyError, IndexError) as e:
                    logger.warning("Unexpected chunk structure: %s — %s", data[:80], e)
                    continue
    except httpx.TimeoutException as e:
        logger.error("vLLM streaming timed out for generation_id=%s: %s", req.generation_id, e)
        yield "[ERROR] Request timed out"
    except httpx.ConnectError as e:
        logger.error("Cannot connect to vLLM for streaming: %s", e)
        yield "[ERROR] Cannot connect to inference server"
    except Exception as e:
        logger.error("Unexpected error during streaming for generation_id=%s: %s", req.generation_id, e, exc_info=True)
        yield f"[ERROR] Unexpected error: {str(e)}"

@app.get("/health")
async def health():
    health_status = {"status": "ok", "redis": "unknown", "vllm": "unknown"}

    # Check Redis
    try:
        r.ping()
        health_status["redis"] = "connected"
    except Exception as e:
        health_status["redis"] = "disconnected"
        logger.warning("Health check: Redis is down — %s", e)

    logger.debug("Health check responded: %s", health_status)
    return health_status


@app.post("/analyze/stream")
@limiter.limit("100000000000000000000000/minute")
async def analyze(request: Request, body: StreamVLMRequest, api_key: str = Depends(verify_api_key)):
    request_id = body.generation_id
    logger.info("── Request %s ── model=%s, query='%s'", request_id, body.model, body.query[:80])

    # Validate image
    try:
        await validate_image_url(str(body.image_url))
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] Unexpected error during image validation: %s", request_id, e, exc_info=True)
        failed_requests.inc()
        raise HTTPException(status_code=500, detail="Image validation failed unexpectedly")

    total_requests.inc()
    cache_key = generate_cache_key(str(body.image_url), body.query)

    # Check cache
    try:
        cached = r.hgetall(cache_key)
    except redis.ConnectionError as e:
        logger.error("[%s] Redis connection error during cache lookup: %s", request_id, e)
        cached = None
    except Exception as e:
        logger.error("[%s] Unexpected Redis error during cache lookup: %s", request_id, e, exc_info=True)
        cached = None

    if cached:
        logger.info("[%s] Cache HIT — returning cached response", request_id)
        cache_hits.inc()
        response_text = "Using cached response \n\n" + cached['response']

        async def cached_stream():
            words = response_text.split(' ')
            for i in range(0, len(words), 10):
                chunk = ' '.join(words[i:i+10]) + ' '
                yield chunk

        return StreamingResponse(cached_stream(), media_type="text/plain")

    # Cache miss — call vLLM
    logger.info("[%s] Cache MISS — forwarding to vLLM", request_id)
    cache_miss.inc()

    # Pre-flight check: is vLLM reachable?
    try:
        test = await http_client.post(
            VLLM_URL,
            json={
                "model": body.model,
                "messages": [{"role": "user", "content": "ping"}]
            },
            headers=runpod_headers,
            timeout=15.0
        )

        if test.status_code != 200:
            logger.error("[%s] vLLM pre-flight returned HTTP %d — body: %s",
                         request_id, test.status_code, test.text[:200])
            failed_requests.inc()
            raise HTTPException(503, "vLLM unavailable")

        logger.info("[%s] vLLM pre-flight OK (HTTP %d)", request_id, test.status_code)

    except httpx.ConnectError as e:
        logger.error("[%s] vLLM pre-flight connect error: %s", request_id, e)
        failed_requests.inc()
        raise HTTPException(503, "vLLM not reachable")
    except httpx.TimeoutException as e:
        logger.error("[%s] vLLM pre-flight timed out: %s", request_id, e)
        failed_requests.inc()
        raise HTTPException(503, "vLLM timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[%s] Unexpected error during vLLM pre-flight: %s", request_id, e, exc_info=True)
        failed_requests.inc()
        raise HTTPException(503, "vLLM health check failed")

    # Stream the response
    async def monitored_stream():
        start = time.time()
        full_response = ""
        token_count = 0

        try:
            async for chunk in stream_generator(body):
                full_response += chunk
                token_count += 1
                yield chunk

            elapsed = time.time() - start
            inference_time.observe(elapsed)
            logger.info("[%s] Stream finished — %d chunks, %.2fs elapsed", request_id, token_count, elapsed)

            # Cache the response
            try:
                r.hset(cache_key, mapping={"response": full_response})
                logger.info("[%s] Response cached (key=%s…)", request_id, cache_key[:16])
            except redis.ConnectionError as e:
                logger.error("[%s] Failed to cache response — Redis down: %s", request_id, e)
            except Exception as e:
                logger.error("[%s] Failed to cache response: %s", request_id, e, exc_info=True)

        except Exception as e:
            elapsed = time.time() - start
            logger.error("[%s] Stream error after %.2fs: %s", request_id, elapsed, e, exc_info=True)
            failed_requests.inc()
            yield f"\n[ERROR] Stream interrupted: {str(e)}"

    return StreamingResponse(
        monitored_stream(),
        media_type="text/event-stream"
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )

frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
    logger.info("Frontend mounted from: %s", frontend_dir)
else:
    logger.warning("Frontend directory not found at: %s — UI will not be served", frontend_dir)

if __name__ == "__main__":
    logger.info("Starting VLM API server on 0.0.0.0:8001")
    uvicorn.run(app, host="0.0.0.0", port=8001)