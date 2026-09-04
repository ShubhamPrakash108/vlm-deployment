import time
import uuid
import math

from locust import HttpUser, task, between, events, LoadTestShape

class RampUpSteadyRampDown(LoadTestShape):
    """
    Three-phase load shape over 5 minutes:
      Phase 1 (0–100s):   Linear ramp from 0 to 50 users
      Phase 2 (100–200s): Hold steady at 50 users
      Phase 3 (200–300s): Linear ramp from 50 to 0 users
    """

    PEAK_USERS    = 50
    RAMP_UP_TIME  = 100    # seconds
    STEADY_TIME   = 100    # seconds
    RAMP_DOWN_TIME = 100   # seconds
    TOTAL_TIME    = RAMP_UP_TIME + STEADY_TIME + RAMP_DOWN_TIME  # 300s = 5 min

    # How many users to add/remove per tick (Locust checks every ~1s)
    SPAWN_RATE = 1  # users per second smoothly

    def tick(self):
        run_time = self.get_run_time()

        if run_time > self.TOTAL_TIME:
            return None  # stop the test

        if run_time <= self.RAMP_UP_TIME:
            # Phase 1: linear ramp up
            progress = run_time / self.RAMP_UP_TIME
            users = max(1, int(self.PEAK_USERS * progress))
            return (users, self.SPAWN_RATE)

        elif run_time <= self.RAMP_UP_TIME + self.STEADY_TIME:
            # Phase 2: hold at peak
            return (self.PEAK_USERS, self.SPAWN_RATE)

        else:
            # Phase 3: linear ramp down
            elapsed_in_ramp_down = run_time - self.RAMP_UP_TIME - self.STEADY_TIME
            progress = elapsed_in_ramp_down / self.RAMP_DOWN_TIME
            users = max(1, int(self.PEAK_USERS * (1 - progress)))
            return (users, self.SPAWN_RATE)

class VLMUser(HttpUser):
    """
    Simulates a user hitting the VLM API.
    Each user waits 1–3 seconds between requests to simulate
    realistic think-time.
    """
    wait_time = between(1, 3)

    API_KEY = "12345"
    HEADERS = {
        "X-API-Key": "12345",
        "Content-Type": "application/json",
    }

    # A set of realistic test image URLs
    # Using safe images that don't violate size limits or alpha transparency issues
    TEST_IMAGES = [
        "https://cdn1.byjus.com/wp-content/uploads/2021/03/bar-graph.png"
    ]

    TEST_QUERIES = [
        "Describe this image in detail.",
        "What objects do you see in this image?",
        "What colors are prominent in this image?",
        "Is there any text visible in this image?",
        "What is the main subject of this image?",
    ]

    @task(5)
    def health_check(self):
        """GET /health — lightweight, no auth required."""
        with self.client.get("/health", name="/health", catch_response=True) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") != "ok":
                    resp.failure(f"Health not ok: {data}")
            else:
                resp.failure(f"HTTP {resp.status_code}")

    @task(1)
    def analyze_stream(self):
        """POST /analyze/stream — the main inference endpoint."""
        import random
        image_url = random.choice(self.TEST_IMAGES)
        # Append a unique UUID so each request is different and bypasses the cache
        query = f"{random.choice(self.TEST_QUERIES)} {uuid.uuid4()}"

        payload = {
            "generation_id": str(uuid.uuid4()),
            "model": "shubhamprakash108/chartqa-vlm-vllm",
            "image_url": image_url,
            "query": query,
        }

        with self.client.post(
            "/analyze/stream",
            json=payload,
            headers=self.HEADERS,
            name="/analyze/stream",
            catch_response=True,
            stream=True,
            timeout=120,
        ) as resp:
            if resp.status_code == 200:
                # Consume the full stream to measure end-to-end latency
                content = b""
                for chunk in resp.iter_content(chunk_size=1024):
                    content += chunk
                
                # Print the query and the resulting response so we can see it live!
                decoded_text = content.decode(errors='replace')
                print(f"\n [STREAM SUCCESS]")
                print(f"Query: {query}")
                print(f"Response: {decoded_text.strip()[:600]}") # Show up to 600 chars


                if b"[ERROR]" in content:
                    resp.failure(f"Stream error: {decoded_text[:200]}")
            elif resp.status_code == 429:
                resp.failure("Rate limited (429)")
            elif resp.status_code == 503:
                resp.failure("vLLM unavailable (503)")
            else:
                resp.failure(f"HTTP {resp.status_code}")

@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """Print p50 / p95 / p99 summary when the test ends."""
    stats = environment.runner.stats

    print("\n" + "=" * 80)
    print("  PERCENTILE LATENCY REPORT (milliseconds)")
    print("=" * 80)
    print(f"{'Endpoint':<30} {'p50':>10} {'p95':>10} {'p99':>10} {'Requests':>10} {'Failures':>10}")
    print("-" * 80)

    for entry in stats.entries.values():
        if entry.num_requests == 0:
            continue
        p50 = entry.get_response_time_percentile(0.50) or 0
        p95 = entry.get_response_time_percentile(0.95) or 0
        p99 = entry.get_response_time_percentile(0.99) or 0
        print(
            f"{entry.name:<30} "
            f"{p50:>10.0f} "
            f"{p95:>10.0f} "
            f"{p99:>10.0f} "
            f"{entry.num_requests:>10} "
            f"{entry.num_failures:>10}"
        )

    # Aggregated
    total = stats.total
    if total.num_requests > 0:
        print("-" * 80)
        p50 = total.get_response_time_percentile(0.50) or 0
        p95 = total.get_response_time_percentile(0.95) or 0
        p99 = total.get_response_time_percentile(0.99) or 0
        print(
            f"{'Aggregated':<30} "
            f"{p50:>10.0f} "
            f"{p95:>10.0f} "
            f"{p99:>10.0f} "
            f"{total.num_requests:>10} "
            f"{total.num_failures:>10}"
        )
    print("=" * 80 + "\n")
