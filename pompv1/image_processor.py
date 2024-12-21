# File: /pompv1/image_processor.py

import os
import time
import json
import redis
import requests
import logging
import base64
from io import BytesIO
from PIL import Image
from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO
from dotenv import load_dotenv
from supabase import create_client, Client

from openai_decider import get_decision

# Import your separate coin uploader (dedicated for "yes" coins)
from cloudflare_uploader_coins import upload_yes_coin_png

load_dotenv()

logging.basicConfig(level=logging.INFO)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6380/0")

if not SUPABASE_URL or not SUPABASE_KEY:
    logging.error("SUPABASE_URL or SUPABASE_KEY not set.")
    exit(1)

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logging.error(f"Failed to create Supabase client: {e}", exc_info=True)
    exit(1)

try:
    r = redis.from_url(REDIS_URL)
except Exception as e:
    logging.error(f"Failed to connect to Redis: {e}", exc_info=True)
    exit(1)

app = Flask(__name__, static_url_path='/static', static_folder='frontend')
socketio = SocketIO(app, cors_allowed_origins="*")

IMG_WIDTH = 512
IMG_HEIGHT = 512
GRID_COLS = 2
GRID_ROWS = 4
BOX_WIDTH = IMG_WIDTH // GRID_COLS
BOX_HEIGHT = IMG_HEIGHT // GRID_ROWS

current_bundle_id = None


def process_next_bundle():
    """
    Continuously checks the 'bundle_queue' in Redis for the next item (bundle).
    When found, downloads the bundle's main image, splits it into 8 coins, 
    updates the front-end, runs the OpenAI decision, then for each "yes" coin:
    - inserts into 'goodcoins'
    - uploads the PNG to Cloudflare
    - updates 'goodcoins.cloudflareimage' with the final public URL
    """
    global current_bundle_id
    item = r.lpop("bundle_queue")
    if not item:
        logging.info("No item found in bundle_queue.")
        return

    logging.info(f"Pulled item from queue: {item}")

    try:
        data = json.loads(item)
    except json.JSONDecodeError:
        logging.error("Invalid JSON in queue item.")
        return

    bundle_id = data.get("bundle_id")
    image_url = data.get("image_url")
    logging.info(f"Starting process for bundle {bundle_id} with image_url {image_url}")

    if not bundle_id or not image_url:
        logging.error("Bundle data missing bundle_id or image_url.")
        return

    current_bundle_id = bundle_id

    # 1) Download main image
    logging.info("Downloading image...")
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        logging.info("Image downloaded and opened successfully.")
    except requests.RequestException as e:
        logging.error(f"Failed to download image for bundle {bundle_id}: {e}", exc_info=True)
        return
    except Exception as e:
        logging.error(f"Error processing image for bundle {bundle_id}: {e}", exc_info=True)
        return

    # 2) Where do we store the 8 coin images?
    bundle_dir = os.path.join(os.path.dirname(__file__), "frontend", "coins", bundle_id)
    os.makedirs(bundle_dir, exist_ok=True)

    # 3) Split into 8 coins
    coins_data = []
    for i in range(8):
        row = i // GRID_COLS
        col = i % GRID_COLS
        x = col * BOX_WIDTH
        y = row * BOX_HEIGHT
        try:
            coin_img = img.crop((x, y, x + BOX_WIDTH, y + BOX_HEIGHT))
            coin_file_name = f"{i+1:02d}.png"
            coin_path = os.path.join(bundle_dir, coin_file_name)
            coin_img.save(coin_path, format='PNG')
            logging.info(f"Cropped and saved coin {i+1} to {coin_path}")
            coins_data.append({
                "id": f"{i+1:02d}",
                "url": f"/static/coins/{bundle_id}/{coin_file_name}"
            })
        except Exception as e:
            logging.error(f"Error cropping coin {i+1} from bundle {bundle_id}: {e}", exc_info=True)
            return

    # 4) Send them to front-end
    try:
        socketio.emit("clear_canvas", {"bundle_id": bundle_id})
        for c in coins_data:
            socketio.emit("add_coin", {"bundle_id": bundle_id, "id": c["id"], "url": c["url"]})
    except Exception as e:
        logging.error(f"Error sending coins to frontend: {e}", exc_info=True)
        return

    # 5) Fetch coin metadata from Supabase
    try:
        res = supabase.table('coins').select("*").eq('bundle_id', bundle_id).execute()
        coin_rows = res.data
        coin_rows.sort(key=lambda x: x['coin_id'])
        coin_info_list = []
        for c in coin_rows:
            coin_info_list.append({
                "id": c['coin_id'],
                "name": c.get('metadata_name', ''),
                "symbol": c.get('metadata_symbol', ''),
                "description": c.get('metadata_description', '')
            })
        logging.info("Fetched coin metadata from Supabase.")
    except Exception as e:
        logging.error(f"Error fetching coin metadata for bundle {bundle_id}: {e}", exc_info=True)
        return

    # 6) OpenAI decisions (yes/no)
    logging.info("Requesting decisions from OpenAI...")
    decisions = get_decision(bundle_id, image_url, coin_info_list)
    logging.info(f"OpenAI decisions: {decisions}")
    if decisions is None:
        logging.error(f"No valid OpenAI decisions for bundle {bundle_id}.")
        return

    # 7) For each "yes" coin => insert row in 'goodcoins', 
    #    upload its PNG to Cloudflare, update 'cloudflareimage'.
    yes_coins = [d for d in decisions if d['decision'] == 'yes']
    for yc in yes_coins:
        coin_id = yc['id']
        coin_filename = f"{coin_id}.png"
        local_path = os.path.join(bundle_dir, coin_filename)
        if not os.path.isfile(local_path):
            logging.error(f"Coin image not found: {local_path}")
            continue

        # find coin_uuid in the 'coins' table
        try:
            coin_info_resp = supabase.table('coins') \
                .select('id') \
                .eq('bundle_id', bundle_id) \
                .eq('coin_id', coin_id) \
                .execute()
            if coin_info_resp.data and len(coin_info_resp.data) > 0:
                coin_uuid = coin_info_resp.data[0]['id']

                # Insert row into goodcoins
                gc_insert_resp = supabase.table('goodcoins').insert({
                    "coin_uuid": coin_uuid
                }).execute()

                if not gc_insert_resp.data:
                    logging.error(f"Failed to insert new row in goodcoins for coin_uuid={coin_uuid}.")
                    continue

                goodcoin_id = gc_insert_resp.data[0]['id']

                # Use the new coin-uploader function for "yes" coins
                uploaded_url = upload_yes_coin_png(local_path, f"{goodcoin_id}.png")
                if uploaded_url:
                    supabase.table('goodcoins') \
                        .update({"cloudflareimage": uploaded_url}) \
                        .eq("id", goodcoin_id) \
                        .execute()
                    logging.info(f"Updated goodcoins id={goodcoin_id} with image={uploaded_url}")
                else:
                    logging.warning(f"Coin upload failed for coin_uuid={coin_uuid}")
            else:
                logging.warning(f"Could not find coin row for (bundle_id={bundle_id}, coin_id={coin_id}).")
        except Exception as e:
            logging.error(f"Error handling 'yes' coin for coin_id={coin_id}: {e}", exc_info=True)

    # 8) Emit overlay marks & fade out
    try:
        socketio.emit("overlay_marks", decisions)
        time.sleep(1)
        socketio.emit("fade_out", {})
    except Exception as e:
        logging.error(f"Error overlaying marks/fading out: {e}", exc_info=True)

    current_bundle_id = None
    logging.info(f"Completed processing for bundle {bundle_id}")


@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/disqualify_coin', methods=['POST'])
def disqualify_coin():
    """
    Endpoint called by newcoincheck to inform the front-end 
    that a coin has been disqualified (will display big red text).
    """
    data = request.get_json()
    coin_id = data.get("coin_id")
    if coin_id:
        socketio.emit("disqualified_coin", {"coin_id": coin_id})
        return {"status": "ok"}, 200
    return {"status": "missing coin_id"}, 400


@app.route('/upload_screenshot', methods=['POST'])
def upload_screenshot():
    """
    Endpoint used to upload a screenshot to the main bucket
    (non-lens route). 
    """
    data = request.get_json()
    if not data or 'base64' not in data or 'filename' not in data:
        return {"success": False, "message": "Missing base64 or filename"}, 400

    image_b64 = data['base64']
    filename = data['filename']
    try:
        raw_bytes = base64.b64decode(image_b64)
        temp_path = f"temp_{filename}"
        with open(temp_path, "wb") as f:
            f.write(raw_bytes)

        # Typically you'd call your existing cloudflare_uploader.py here,
        # or the new coin or lens uploaders, depending on your preference.
        from cloudflare_uploader import upload_to_cloudflare
        uploaded_url = upload_to_cloudflare(temp_path, filename)
        if not uploaded_url:
            return {"success": False, "message": "Cloudflare upload failed"}, 500

        return {"success": True, "cloudflareUrl": uploaded_url}, 200

    except Exception as e:
        logging.error(f"Error in /upload_screenshot: {e}", exc_info=True)
        return {"success": False, "message": str(e)}, 500


@app.route('/upload_screenshot_lens', methods=['POST'])
def upload_screenshot_lens():
    """
    Endpoint used by Puppeteer to upload lens screenshots. 
    That calls your cloudflare_uploader_lens.py with the lens domain.
    """
    data = request.get_json()
    if not data or 'base64' not in data or 'filename' not in data:
        return {"success": False, "message": "Missing base64 or filename"}, 400

    image_b64 = data['base64']
    filename = data['filename']
    try:
        raw_bytes = base64.b64decode(image_b64)
        temp_path = f"temp_{filename}"
        with open(temp_path, "wb") as f:
            f.write(raw_bytes)

        from cloudflare_uploader_lens import upload_lens_screenshot
        uploaded_url = upload_lens_screenshot(temp_path, filename)
        if not uploaded_url:
            return {"success": False, "message": "Lens Cloudflare upload failed"}, 500

        return {"success": True, "cloudflareUrl": uploaded_url}, 200

    except Exception as e:
        logging.error(f"Error in /upload_screenshot_lens: {e}", exc_info=True)
        return {"success": False, "message": str(e)}, 500


# --------------- Additional endpoints for the "active investigation" overlay ---------------
@app.route('/start_investigation', methods=['POST'])
def start_investigation():
    """
    Called by newcoincheck.py to show a coin's image 
    in the center of the watermill feed UI while we do lens/twitter checks.
    """
    data = request.get_json()
    image_url = data.get("image_url")
    if not image_url:
        return jsonify({"error": "no image_url provided"}), 400

    socketio.emit("start_investigation", {"image_url": image_url})
    return jsonify({"status": "ok"}), 200

@app.route('/stop_investigation', methods=['POST'])
def stop_investigation():
    """
    Called by newcoincheck.py to hide the "active investigation" image
    once the coin is either disqualified or we proceed to "buy."
    """
    socketio.emit("stop_investigation", {})
    return jsonify({"status": "ok"}), 200
# ------------------------------------------------------------------------------------


def run_processor():
    """
    Background loop that continuously processes the next bundle in the queue.
    """
    while True:
        process_next_bundle()
        time.sleep(5)


if __name__ == "__main__":
    import threading
    t = threading.Thread(target=run_processor, daemon=True)
    t.start()
    socketio.run(app, host="0.0.0.0", port=5000)
