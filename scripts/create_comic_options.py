#!/usr/bin/env python3
"""
Create series-based options.yml for TRMNL Comic Covers Plugin
"""

import yaml
import json
from pathlib import Path
import logging

# Ensure yaml keeps mapping order
yaml.add_representer(dict, lambda dumper, data: dumper.represent_mapping("tag:yaml.org,2002:map", data.items()))


def create_comic_options_yml():
    """
    Create series-based options.yml for Comic Covers plugin
    """

    # Load datasets
    data_dir = Path(__file__).parent.parent / "data"

    try:
        with open(data_dir / "publishers.json", "r") as f:
            publishers = json.load(f)
        with open(data_dir / "popular_series.json", "r") as f:
            series = json.load(f)
    except Exception as e:
        logging.error(f"Failed to load datasets: {e}")
        return

    # Remove duplicate series
    unique_series = {}
    for s in series:
        series_id = s['id']
        if series_id not in unique_series:
            unique_series[series_id] = s

    # FIRST: Sort by issue count descending to get the most popular
    series_by_popularity = sorted(
        unique_series.values(),
        key=lambda x: x.get('issue_count', 0),
        reverse=True
    )[:250]  # Take top 100 most popular series

    # THEN: Sort the top 100 alphabetically for easy browsing
    sorted_series = sorted(
        series_by_popularity,
        key=lambda x: str(x.get('name', '')).lower()
    )

    # Check for NSFW indicators in the actual data
    def is_nsfw(series_data):
        """Check if series has NSFW indicators in available fields"""
        # Safely handle None values
        name = str(series_data.get('name', '')).lower()
        description = str(series_data.get('description', '')).lower()
        publisher_name = str(series_data.get('publisher_name', '')).lower()

        # Check for mature content indicators
        nsfw_indicators = [
            'mature', 'adult', 'explicit', '18+', 'nsfw',
            'vertigo', 'max', 'black label', 'adults only'
        ]

        # Check publisher for mature imprints
        mature_publishers = ['vertigo', 'avatar', 'idw adult', 'dark horse manga']

        # Check if any NSFW indicators are present
        for indicator in nsfw_indicators:
            if (indicator in name or
                    indicator in description or
                    indicator in publisher_name):
                return True

        # Check for mature publisher imprints
        for publisher in mature_publishers:
            if publisher in publisher_name:
                return True

        return False

    # About field
    about_field = {
        'keyname': 'about',
        'name': 'About This Plugin',
        'field_type': 'author_bio',
        'description':
            f"Display random comic book covers on your TRMNL e-ink device using the Comic Vine API.<br /><br />"
            f"<strong>Series-Based Approach:</strong><br />"
            f"• Select from {len(sorted_series)} most popular comic series<br />"
            f"<strong>Setup:</strong><br />"
            f"1. Get a free API key from <a href='https://comicvine.gamespot.com/api/'>Comic Vine</a><br />"
            f"2. Add your API key to the field below<br />"
            f"3. Select your favorite series (required)<br /><br />",
        'learn_more_url': 'https://comicvine.gamespot.com/api/',
        'github_url': 'https://github.com/ExcuseMi/trmnl-comic-book-covers-plugin',
        'category': 'comics,art'
    }

    # Field definitions
    fields = [
        about_field,

        # API Configuration
        {
            'keyname': 'comic_vine_api_key',
            'field_type': 'password',
            'name': 'Comic Vine API Key',
            'description': 'Your Comic Vine API key. Get a free key at <a href="https://comicvine.gamespot.com/api/" target="_blank">comicvine.gamespot.com/api/</a>',
            'placeholder': 'Enter your API key',
            'optional': False
        },



        # Series Selection (Primary Filter) - MANDATORY
        {
            'keyname': 'selected_series',
            'field_type': 'select',
            'name': 'Comic Series',
            'optional': False,
            'description': 'Select one or more comic series to display covers from. Shows issue counts for reference. This field is required.',
            'multiple': True,
            'options': [
                {
                    f"{s['name']} ({s.get('start_year', 'N/A')}) - {s.get('issue_count', 0)} issues":
                        f"{s['id']}|{s.get('issue_count', 200)}"
                }
                for s in sorted_series
            ],
            'default': [
                f"{sorted_series[0]['id']}|{sorted_series[0].get('issue_count', 200)}"] if sorted_series else [],
            'help_text': "Use <kbd>⌘</kbd>+<kbd>click</kbd> or <kbd>Ctrl</kbd>+<kbd>click</kbd> to select multiple series. At least one series must be selected.",
        },
        # Display Options
        {
            'keyname': 'display_style',
            'field_type': 'select',
            'name': 'Display Style',
            'description': 'Choose how comic information is displayed.',
            'options': [
                {'Full Screen Covers Landscape': 'full|cover|landscape|3'},
                {'Full Screen Details Landscape': 'full|details|landscape|1'},
                {'Full Screen Cover Portrait': 'full|cover|portrait|1'},

                {'Half horizontal Cover Landscape': 'horizontal|cover|landscape|4'},
                {'Half horizontal Details Landscape': 'horizontal|details|landscape|1'},
                {'Half horizontal Cover Portrait': 'horizontal|cover|portrait|2'},

                {'Half vertical Cover Landscape': 'vertical|cover|landscape|1'},
                {'Half vertical Cover Portrait': 'vertical|cover|portrait|1'},

                {'Quadrant Covers Landscape': 'quadrant|cover|landscape|3'},
                {'Quadrant Details Landscape': 'quadrant|details|landscape|1'},
                {'Quadrant Covers Portrait': 'quadrant|cover|portrait|1'},
            ],
            'multiple': False,
            'optional': False,
        },
    ]

    # Count NSFW series for logging
    nsfw_count = sum(1 for s in sorted_series if is_nsfw(s))

    # Calculate average issue count for the top series
    avg_issue_count = sum(s.get('issue_count', 0) for s in sorted_series) / len(sorted_series) if sorted_series else 0

    # Write YAML file
    output_file = Path(__file__).parent.parent / "data" / "options.yml"
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            yaml.dump(fields, f, sort_keys=False, width=1000, allow_unicode=True)
        logging.info(f"✓ Created {output_file}")
        print(f"Generated series-based options.yml with {len(fields)} fields")
        print(f"- {len(publishers)} publishers")
        print(f"- {len(sorted_series)} most popular series (sorted A-Z)")
        print(f"- {nsfw_count} series marked as NSFW")
        print(f"- Average issue count: {avg_issue_count:.1f}")
        print(f"- Series selection is MANDATORY")

    except Exception as e:
        logging.error(f"Failed to write {output_file}: {e}")


if __name__ == "__main__":
    create_comic_options_yml()