import asyncio

active_transfers = {}
message_queue = asyncio.Queue()
connections = {}