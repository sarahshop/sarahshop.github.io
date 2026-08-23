
(function(){
  const C = window.SARAH_SHOP_CONFIG || {};
  const URL = C.supabaseUrl;
  const KEY = C.supabasePublishableKey;

  const ID_MAP = {
    "Netflix|1 Month":"netflix-1m",
    "Netflix|3 Months":"netflix-3m",
    "Netflix|Lifetime":"netflix-life",
    "Crunchyroll|1 Month":"crunchyroll-1m",
    "Crunchyroll|3 Months":"crunchyroll-3m",
    "Crunchyroll|Lifetime":"crunchyroll-life",
    "HBO Max|1 Month":"hbomax-1m",
    "HBO Max|3 Months":"hbomax-3m",
    "HBO Max|Lifetime":"hbomax-life",
    "Spotify|12 Months":"spotify-12m",
    "Spotify|Lifetime":"spotify-life",
    "Prime Video|1 Month":"prime-1m",
    "Prime Video|3 Months":"prime-3m",
    "Prime Video|Lifetime":"prime-life",
    "Paramount+|1 Month":"paramount-1m",
    "Paramount+|3 Months":"paramount-3m",
    "Paramount+|Lifetime":"paramount-life",
    "ChatGPT|1 Month":"chatgpt-1m",
    "ChatGPT|3 Months":"chatgpt-3m",
    "Claude|1 Month":"claude-1m",
    "Claude|3 Months":"claude-3m",
    "Gemini|1 Month":"gemini-1m",
    "Gemini|3 Months":"gemini-3m",
    "Server Boosts|Small Pack":"boost-small",
    "Server Boosts|Medium Pack":"boost-medium",
    "Server Boosts|Large Pack":"boost-large",
    "Real Members|Starter":"members-starter",
    "Real Members|Medium":"members-medium",
    "Real Members|Large":"members-large",
    "TikTok|Basic Package":"tiktok-basic",
    "TikTok|Medium Package":"tiktok-medium",
    "TikTok|Large Package":"tiktok-large",
    "Instagram|Basic Package":"instagram-basic",
    "Instagram|Medium Package":"instagram-medium",
    "Instagram|Large Package":"instagram-large",
    "YouTube|Basic Package":"youtube-basic",
    "YouTube|Medium Package":"youtube-medium",
    "YouTube|Large Package":"youtube-large",
    "Fortnite|Starter":"fortnite-starter",
    "Fortnite|Better Account":"fortnite-better",
    "Fortnite|Premium Account":"fortnite-premium",
    "Valorant|Starter":"valorant-starter",
    "Valorant|Better Account":"valorant-better",
    "Valorant|Premium Account":"valorant-premium"
  };

  function rowToProduct(r){
    return {
      db_id: r.id,
      id: ID_MAP[`${r.name}|${r.option_name}`] || `db-${r.id}`,
      cat: String(r.category || "").toLowerCase(),
      name: r.name,
      option: r.option_name || "",
      price: Number(r.price || 0),
      stock: Number(r.stock || 0),
      image: r.image || "",
      active: r.active !== false
    };
  }

  async function loadProducts(){
    if(!URL || !KEY) throw new Error("Supabase configuration is missing.");
    const endpoint = `${URL}/rest/v1/products?active=eq.true&select=id,name,category,option_name,price,stock,image,active&order=id.asc`;
    const r = await fetch(endpoint, {
      cache: "no-store",
      headers: {
        "apikey": KEY,
        "Authorization": `Bearer ${KEY}`,
        "Accept": "application/json"
      }
    });
    if(!r.ok) throw new Error(`Stock database request failed (${r.status}).`);
    const rows = await r.json();
    return rows.map(rowToProduct);
  }

  window.SARAH_DB = { loadProducts };
})();
