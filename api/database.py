import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

if url is None or key is None:
    raise ValueError("Missing Supabase credentials: 'SUPABASE_URL' and 'SUPABASE_KEY'")

supabase: Client = create_client(url, key)


