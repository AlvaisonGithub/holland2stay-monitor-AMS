"""
tools/test_h2s_api.py — live check of the H2S ZenRows/BrightData path.

Run from the project root:
    .venv\\Scripts\\python.exe tools\\test_h2s_api.py
    (optional) set H2S_FETCH_MODE=zenrows   or   brightdata   to force one provider

Uses 2-4 API requests. Prints which strategy worked and the
"Available to book" / "Available in lottery" units it found.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from scrapers.base import ScrapeTask  # noqa: E402
from scrapers.h2s_api_fetcher import H2SApiFetcher, resolve_h2s_fetch_mode  # noqa: E402
from scrapers.holland2stay import HollandStayScraper  # noqa: E402

print("H2S fetch mode:", resolve_h2s_fetch_mode())
if resolve_h2s_fetch_mode() != "api":
    sys.exit("No ZENROWS_API_KEY / BRIGHTDATA_API_KEY found in this environment's .env "
             "(or H2S_FETCH_MODE=browser). Add a key and run again.")
city_id = sys.argv[1] if len(sys.argv) > 1 else "24"  # 24 = Amsterdam
s = HollandStayScraper()
with s.batch_session():
    res = s.scrape(ScrapeTask(source="holland2stay", city_key=city_id, city_display=f"city {city_id}",
                              extra={"availability_ids": ["179", "336"]}))
print(f"\nStrategy used: {H2SApiFetcher._sticky_strategy}")
print(f"Found {len(res.listings)} units (complete={res.complete})")
for l in res.listings[:20]:
    print(f"  [{l.status}] {l.name} {l.price_raw or ''} from {l.available_from or '?'}  {l.url}")
