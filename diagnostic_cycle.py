from __future__ import annotations
import time
from src.sources.runner import run_all
from src.modules.source_registry.runner import collect_registered_sources
from src.shared.config import get_settings
from src.shared.db import create_session
from src.modules.offers.models import Offer
from sqlalchemy import select, func

def main():
    settings = get_settings()
    print("Starting Diagnostic Cycle...")
    start_time = time.time()
    
    # Run legacy web sources
    legacy_results = run_all(path=settings.sources_config_path)
    
    # Run registry (Telegram) sources
    registry_results = collect_registered_sources()
    
    duration = time.time() - start_time
    
    print("\n--- LEGACY WEB SOURCES ---")
    print(f"{'Source':<20} | {'Fetched':<7} | {'New':<5} | {'Updated':<7} | {'Dedup':<5} | {'Errors':<6} | {'Time':<5}")
    for r in legacy_results:
        print(f"{r.source_key:<20} | {r.fetched:<7} | {r.created:<5} | {r.updated:<7} | {r.duplicates:<5} | {r.errors:<6} | {r.duration_seconds:<5.1f}")

    print("\n--- REGISTRY TELEGRAM SOURCES ---")
    print(f"{'Source':<20} | {'Fetched':<7} | {'New':<5} | {'Updated':<7} | {'Dedup':<5} | {'Errors':<6} | {'Time':<5}")
    for r in registry_results:
        print(f"{r.source_key:<20} | {r.fetched:<7} | {r.offers_created:<5} | {r.offers_updated:<7} | {r.duplicates:<5} | {r.errors:<6} | {r.duration_seconds:<5.1f}")
        
    with create_session() as session:
        total = session.scalar(select(func.count()).select_from(Offer))
    
    print(f"\nTotal offers in DB: {total}")
    print(f"Cycle duration: {duration:.1f}s")

if __name__ == "__main__":
    main()
