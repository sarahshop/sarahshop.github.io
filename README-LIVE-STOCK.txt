SARAH SHOP — LIVE SUPABASE STOCK VERSION

WHAT CHANGED
- Same 3D/glow-bar UI.
- Prices now load from Supabase.
- Stock now loads from Supabase.
- Product cards show total live stock.
- Product option drawer shows stock per option.
- Out-of-stock options cannot be opened.
- Checkout re-checks the current price and stock.
- If Supabase is temporarily unavailable, the old products.js data remains as a fallback.

UPLOAD TO GITHUB
Upload/replace ALL files in this folder in the repository root.

IMPORTANT
This version READS stock/prices only.
The next setup step is saving Order/Invoice IDs into the Supabase `orders` table.
