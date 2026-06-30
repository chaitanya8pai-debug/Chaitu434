import requests, os
from dotenv import load_dotenv
load_dotenv('/Users/vinayaka/Desktop/trading-bot/.env')

TOKEN   = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

print('Token  :', TOKEN[:15] if TOKEN else 'NOT FOUND', '...')
print('Chat ID:', CHAT_ID)

r = requests.post(
    f'https://api.telegram.org/bot{TOKEN}/sendMessage',
    json={
        'chat_id': CHAT_ID,
        'text': 'Chaitu Trading Bot is LIVE! Gold, BTC, ETH signals coming your way!'
    }
)
result = r.json()
print('Status:', r.status_code)
if result.get('ok'):
    print('SUCCESS - Check your Telegram now!')
else:
    print('Error:', result.get('description'))
