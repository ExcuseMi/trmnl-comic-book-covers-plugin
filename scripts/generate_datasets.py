#!/usr/bin/env python3
"""
Comic Covers Dataset Generator for TRMNL Plugin
Fetches most popular series from top publishers from Comic Vine API
"""

import json
import os
import requests
from pathlib import Path
from dotenv import load_dotenv
import time

MAX_SERIES = 5000
MAX_PUBLISHERS = 100  # Limit to top 100 publishers
# Load environment variables
load_dotenv()


class ComicVineDataGenerator:
    def __init__(self):
        self.api_key = os.getenv('COMIC_VINE_API_KEY')
        self.base_url = "https://comicvine.gamespot.com/api"
        self.headers = {
            'User-Agent': 'TRMNL-Comic-Covers/1.0'
        }

        if not self.api_key:
            raise ValueError("COMIC_VINE_API_KEY not found in .env file")

    def make_api_call(self, endpoint, params=None):
        """Make API call with rate limiting and error handling"""
        if params is None:
            params = {}

        params.update({
            'api_key': self.api_key,
            'format': 'json'
        })

        url = f"{self.base_url}/{endpoint}"
        print(f"  API Call: {endpoint} with params: { {k: v for k, v in params.items() if k != 'api_key'} }")

        try:
            response = requests.get(url, params=params, headers=self.headers, timeout=30)
            print(f"  Response status: {response.status_code}")

            if response.status_code != 200:
                print(f"  ❌ API Error: {response.status_code} - {response.text[:200]}")
                return None

            response.raise_for_status()
            data = response.json()

            if 'results' in data:
                print(f"  ✅ Got {len(data['results'])} results")
            else:
                print(f"  ❌ No 'results' key in response")
                print(f"  Response keys: {list(data.keys())}")

            # Respect rate limits
            time.sleep(1)

            return data

        except requests.exceptions.RequestException as e:
            print(f"  ❌ Request Exception: {e}")
            return None
        except json.JSONDecodeError as e:
            print(f"  ❌ JSON Decode Error: {e}")
            return None

    def safe_get(self, obj, key, default=""):
        """Safely get a value from dictionary and ensure it's a string"""
        if not obj or not isinstance(obj, dict):
            return default

        value = obj.get(key, default)
        if value is None:
            return default

        try:
            return str(value)
        except:
            return default
    def fetch_most_popular_series(self):
        """Fetch the most popular series by issue count directly"""
        print("Fetching most popular series directly...")

        all_series = []
        offset = 0
        limit = 100

        while len(all_series) < MAX_SERIES:
            print(f"Fetching popular series batch at offset {offset}...")

            data = self.make_api_call('volumes', {
                'offset': offset,
                'limit': limit,
                'sort': 'count_of_issues:desc',
                'field_list': 'id,name,count_of_issues,start_year,description,image,api_detail_url,publisher'
            })

            if not data or 'results' not in data or not data['results']:
                print("No more results available")
                break

            batch_added = 0
            for volume in data['results']:
                if len(all_series) >= MAX_SERIES:
                    break

                # Skip if volume is None or missing basic data
                if not volume or not isinstance(volume, dict):
                    print(f"  ⚠️ Skipping invalid volume data: {volume}")
                    continue

                try:
                    series_id = volume.get('id')
                    series_name = self.safe_get(volume, 'name').strip()

                    if not series_id or not series_name:
                        continue

                    # Check if series has any issues at all
                    issue_count = volume.get('count_of_issues', 0)
                    if issue_count < 1:
                        continue

                    # Get publisher info safely
                    publisher = volume.get('publisher', {}) or {}
                    publisher_id = publisher.get('id') if publisher else None
                    publisher_name = self.safe_get(publisher, 'name', 'Unknown')

                    # Get image safely
                    image = volume.get('image', {}) or {}
                    image_url = image.get('small_url', '') if image else ''

                    all_series.append({
                        'id': series_id,
                        'name': series_name,
                        'publisher_id': publisher_id,
                        'publisher_name': publisher_name,
                        'start_year': volume.get('start_year', 0),
                        'issue_count': issue_count,
                        'description': self.safe_get(volume, 'description'),
                        'image': image_url,
                        'api_detail_url': self.safe_get(volume, 'api_detail_url')
                    })
                    batch_added += 1

                except Exception as e:
                    print(f"  ⚠️ Error processing volume {volume.get('id', 'unknown')}: {e}")
                    continue

            print(f"  → Added {batch_added} series in this batch (total: {len(all_series)})")

            offset += limit
            if len(data['results']) < limit:
                break

        return all_series
    def fetch_top_publishers_from_series(self, series_list):
        """Extract top publishers from the series list"""
        publisher_counts = {}

        for series in series_list:
            publisher_id = series['publisher_id']
            publisher_name = series['publisher_name']

            if publisher_id:
                key = (publisher_id, publisher_name)
                publisher_counts[key] = publisher_counts.get(key, 0) + 1

        # Convert to publisher list
        publishers = []
        for (publisher_id, publisher_name), count in sorted(publisher_counts.items(), key=lambda x: x[1], reverse=True)[
                                                     :MAX_PUBLISHERS]:
            publishers.append({
                'id': publisher_id,
                'name': publisher_name,
                'api_name': publisher_name,
                'deck': '',
                'description': '',
                'issue_count': count,  # Approximate from series count
                'image': ''
            })

        return publishers

    def generate_all_datasets(self):
        """Generate all datasets - simplified approach"""
        print("Starting dataset generation...")

        # Fetch most popular series directly
        popular_series = self.fetch_most_popular_series()

        # Extract publishers from the series data
        publishers = self.fetch_top_publishers_from_series(popular_series)

        eras = self.generate_eras_dataset()

        # Save datasets
        self.save_dataset(publishers, "publishers.json")
        self.save_dataset(popular_series, "popular_series.json")
        self.save_dataset(eras, "eras.json")

        # Generate summary
        publisher_breakdown = {}
        for series in popular_series:
            pub_name = series['publisher_name']
            publisher_breakdown[pub_name] = publisher_breakdown.get(pub_name, 0) + 1

        summary = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "publishers_count": len(publishers),
            "series_count": len(popular_series),
            "eras_count": len(eras),
            "method": "direct_popular_series",
            "publisher_breakdown": publisher_breakdown
        }

        self.save_dataset(summary, "dataset_summary.json")
        print("Dataset generation complete!")

        # Print publisher breakdown
        print("\n=== Publisher Breakdown ===")
        for pub_name, count in sorted(publisher_breakdown.items(), key=lambda x: x[1], reverse=True)[:20]:
            print(f"{pub_name}: {count} series")

        return summary
    def generate_eras_dataset(self):
        """Generate comic book eras dataset"""
        return [
            {
                "name": "golden_age",
                "display_name": "Golden Age",
                "start_year": 1938,
                "end_year": 1956,
                "description": "The dawn of superhero comics"
            },
            {
                "name": "silver_age",
                "display_name": "Silver Age",
                "start_year": 1956,
                "end_year": 1970,
                "description": "Revival and modernization of superheroes"
            },
            {
                "name": "bronze_age",
                "display_name": "Bronze Age",
                "start_year": 1970,
                "end_year": 1985,
                "description": "Darker stories and social relevance"
            },
            {
                "name": "modern_age",
                "display_name": "Modern Age",
                "start_year": 1985,
                "end_year": None,
                "description": "Contemporary comic storytelling"
            }
        ]

    def save_dataset(self, data, filename):
        """Save dataset to JSON file"""
        data_dir = Path(__file__).parent.parent / "data"
        data_dir.mkdir(exist_ok=True)

        filepath = data_dir / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"Saved {len(data)} items to {filepath}")



def main():
    try:
        generator = ComicVineDataGenerator()
        summary = generator.generate_all_datasets()

        print("\n=== Dataset Summary ===")
        print(f"Publishers: {summary['publishers_count']}")
        print(f"Series: {summary['series_count']}")
        print(f"Eras: {summary['eras_count']}")
        print(f"Method: {summary['method']}")
        print(f"Generated: {summary['generated_at']}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        print("Make sure your COMIC_VINE_API_KEY is set in the .env file")
if __name__ == "__main__":
    main()