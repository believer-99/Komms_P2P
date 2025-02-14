async def user_input():
    """Handles user input and sends messages to all connected peers."""
    help_text = """
Available commands:
/send <file_path> - Send a file to all connected peers
/pause <file_name> - Pause a file transfer
/resume <file_name> - Resume a paused transfer
/transfers - List all transfers and their status
/help - Show this help message
"""
    
    while True:
        try:
            message = await ainput("> ")
            
            if message.startswith("/"):
                parts = message.split(maxsplit=1)
                command = parts[0]
                args = parts[1] if len(parts) > 1 else ""

                if command == "/help":
                    print(help_text)
                    continue
                    
                elif command == "/transfers":
                    transfers = file_transfer_manager.list_transfers()
                    if not transfers:
                        print("No active or paused transfers.")
                    else:
                        print("\nCurrent Transfers:")
                        print("-" * 50)
                        for file_id, state in transfers.items():
                            progress = len(state.get('sent_chunks', set())) / state['total_chunks'] * 100
                            status = state['status'].upper()
                            print(f"File: {file_id}")
                            print(f"Status: {status}")
                            print(f"Progress: {progress:.1f}%")
                            print("-" * 50)
                    continue
                    
                elif command == "/pause":
                    if not args:
                        print("Usage: /pause <file_name>")
                        continue
                    
                    found = False
                    for file_id in list(file_transfer_manager.active_transfers.keys()):
                        if args in file_id:
                            await file_transfer_manager.pause_transfer(file_id)
                            found = True
                            break
                    
                    if not found:
                        print(f"No active transfer found for '{args}'")
                    continue
                    
                elif command == "/resume":
                    if not args:
                        print("Usage: /resume <file_name>")
                        continue
                    
                    found = False
                    for file_id in list(file_transfer_manager.paused_transfers.keys()):
                        if args in file_id:
                            await file_transfer_manager.resume_transfer(file_id)
                            found = True
                            break
                    
                    if not found:
                        print(f"No paused transfer found for '{args}'")
                    continue
                    
                elif command == "/send":
                    if not args:
                        print("Usage: /send <file_path>")
                        continue

                    if connections:
                        for peer_ip, websocket in list(connections.items()):
                            await file_transfer_manager.send_file(args, websocket, peer_ip)
                    else:
                        print("No peers connected to send file to.")
                    continue
            
            # Handle regular messages (existing code)
            if connections:
                for peer_ip, websocket in list(connections.items()):
                    try:
                        await websocket.send(f"MESSAGE {message}")
                    except Exception as e:
                        logging.exception(f"Error sending to {peer_ip}: {e}")
                        if peer_ip in connections:
                            del connections[peer_ip]
            else:
                print("No peers connected to send message to.")
                
        except Exception as e:
            logging.exception(f"Error in user_input: {e}")
            await asyncio.sleep(1)