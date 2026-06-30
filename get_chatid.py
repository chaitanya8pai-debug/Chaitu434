import requests, time

TOKEN = '8771602305:AAFTv7perccIyNUagNerFBUc-793aXdQhCE'

print("="*50)
print("WAITING FOR YOUR MESSAGE...")
print("="*50)
print("")
print("Go to Telegram RIGHT NOW and:")
print("1. Search your NEW bot username")
print("2. Tap START")
print("3. Send 'hello'")
print("")
print("This script will auto-detect it...")
print("")

for attempt in range(12):
    r = requests.get(f'https://api.telegram.org/bot{TOKEN}/getUpdates')
    data = r.json()
    messages = [u for u in data.get('result', []) if 'message' in u]
    if messages:
        last = messages[-1]
        chat_id = last['message']['chat']['id']
        name    = last['message']['chat'].get('first_name', '')
        print(f"SUCCESS! Found you!")
        print(f"Your name  : {name}")
        print(f"Your Chat ID: {chat_id}")
        print("")
        print("Now run this command:")
        print(f'sed -i \'\' \'s/TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID={chat_id}/\' ~/Desktop/trading-bot/.env')
        print(f'sed -i \'\' \'s/TELEGRAM_BOT_TOKEN=.*/TELEGRAM_BOT_TOKEN={TOKEN}/\' ~/Desktop/trading-bot/.env')
        break
    print(f"Waiting... (attempt {attempt+1}/12 — send hello to your bot on Telegram)")
    time.sleep(5)
else:
    print("Could not find message. Make sure you sent 'hello' to the correct new bot.")
