# My agent: CarDex (SpotterDex)
One-liner: A conversational agent that helps car spotters identify, catalogue, and score real-world car sightings with a catalog of production models, special editions, and one-of-one hypercars.

Tool coverage:
- Memory: User's personal garage (spotted CarDex history), total spotter points, spot streaks, personal counts per vehicle model, and submitted dispute/review history.
- Tools:
  - `identify_and_spot_car`: Multimodal vehicle vision identification (make, model, trim, colorway, special edition status) and logging new spots.
  - `lookup_car_database`: Queries the database of production models, specs, rarity tiers, and Shazam-style global spotted counts.
  - `get_user_garage`: Retrieves the user's collection of spotted cars with personal counts, timestamps, and filter options.
  - `get_leaderboard`: Returns global and regional spotter leaderboards ranked by total points, unique finds, and rarest spots.
  - `submit_spot_review`: Handles dispute and review submissions when a car classification needs human or expert re-evaluation.
- Catalog/UI: Rich A2UI cards displaying car details (badge, colorway, rarity tier, global spot count vs. personal count, points earned, and technical specs), leaderboard ranking tables, and review confirmation modals.
- Image gen: Generates custom commemorative collector cards, rarity badge graphics, or stylized hero renders for one-off and legendary cars.
- Sandbox: Computes dynamic rarity multipliers based on global spot scarcity and calculates spotter leaderboard rankings.

Recommended for every project: memory, storage, tools, image generation, A2UI
Agent-specific / stretch (pick what fits): Code sandbox for rarity scoring calculation, Google Maps API for geocoding spotted locations, Vertex AI Multimodal vision.
