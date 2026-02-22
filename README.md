# Job Signal Bot (Gmail → Discord)

Discord bot that polls gmail for jobright listings digest emails, parses for jobs listed inside, posts each job
into a discord channel in a neat format

---

## What it does

Example format:

🔔  
Sales Development Associate  
📍 Houston, TX 
- company: Gartner Consulting  
- match: 80%  
- pay: $44K/yr - $55K/yr  
- source: Jobright  
- link: Job link

create a google cloud project, get Gmail API
Create OAuth Credentials, application type: Desktop App, add credentials.json to folder
run authtoken.py to get token.json
create discord bot, set permissions to read messages, send messages, embed links, invite bot
pip install -r requirements.txt
make a .env and paste following

DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_ID=
GMAIL_LABEL_NAME=

