import json
import logging
from websockets.connection import State

# MODIFIED: Import locks as well
from networking.shared_state import (
    connections, groups, user_data, message_queue, 
    connections_lock, groups_lock, pending_lock
)
from networking.utils import get_own_ip # Assuming utils.py exists

logger = logging.getLogger(__name__)

# Modify each function to potentially put status/error messages on the queue

async def send_group_create_message(groupname):
    own_ip = await get_own_ip()
    message = json.dumps({"type": "GROUP_CREATE", "groupname": groupname, "admin_ip": own_ip})
    logger.info(f"Broadcasting GROUP_CREATE for '{groupname}'")
    
    # Use lock when accessing connections
    async with connections_lock:
        for ws in connections.values():
            if ws.state == State.OPEN:
                try: await ws.send(message)
                except Exception as e: logger.error(f"Failed send GROUP_CREATE to a peer: {e}")
                
    # ADDED: Notify GUI (optional confirmation)
    await message_queue.put({"type": "log", "message": f"Group '{groupname}' created locally."})
    # Actual confirmation comes when members receive update

async def send_group_invite_message(groupname, peer_ip):
    own_ip = await get_own_ip()
    message = json.dumps({"type": "GROUP_INVITE", "groupname": groupname, "inviter_ip": own_ip})
    ws = connections.get(peer_ip)
    if ws and ws.state == State.OPEN:
        try:
             await ws.send(message)
             logger.info(f"Sent GROUP_INVITE for '{groupname}' to {peer_ip}")
             # ADDED: Notify GUI
             await message_queue.put({"type": "log", "message": f"Sent invite for '{groupname}' to peer."})
        except Exception as e:
             logger.error(f"Failed send GROUP_INVITE to {peer_ip}: {e}")
             await message_queue.put({"type": "log", "message": f"Failed to send invite: {e}", "level": logging.ERROR})
    else:
         logger.warning(f"Cannot send GROUP_INVITE: Peer {peer_ip} not connected.")
         await message_queue.put({"type": "log", "message": f"Cannot send invite: Peer not connected.", "level": logging.WARNING})


async def send_group_invite_response(groupname, inviter_ip, accepted):
    own_ip = await get_own_ip()
    message = json.dumps({"type": "GROUP_INVITE_RESPONSE", "groupname": groupname, "invitee_ip": own_ip, "accepted": accepted})
    ws = connections.get(inviter_ip)
    action = "Accepted" if accepted else "Declined"
    if ws and ws.state == State.OPEN:
        try:
            await ws.send(message)
            logger.info(f"Sent GROUP_INVITE_RESPONSE ({action}) for '{groupname}' to {inviter_ip}")
            # ADDED: Notify GUI
            await message_queue.put({"type": "log", "message": f"{action} invite for group '{groupname}'."})
            # Trigger state updates (handled by receiving end usually, but might need local too)
            await message_queue.put({"type": "group_list_update"})
            await message_queue.put({"type": "pending_invites_update"})
        except Exception as e:
             logger.error(f"Failed send GROUP_INVITE_RESPONSE to {inviter_ip}: {e}")
             await message_queue.put({"type": "log", "message": f"Failed to send invite response: {e}", "level": logging.ERROR})
    else:
        logger.warning(f"Cannot send GROUP_INVITE_RESPONSE: Inviter {inviter_ip} not connected.")
        await message_queue.put({"type": "log", "message": f"Cannot send invite response: Inviter not connected.", "level": logging.WARNING})
        # Remove invite locally anyway if peer disconnected?
        await message_queue.put({"type": "pending_invites_update"})


async def send_group_join_request(groupname, admin_ip):
    own_ip = await get_own_ip()
    message = json.dumps({"type": "GROUP_JOIN_REQUEST", "groupname": groupname, "requester_ip": own_ip, "requester_username": user_data["original_username"]})
    ws = connections.get(admin_ip)
    if ws and ws.state == State.OPEN:
        try:
            await ws.send(message)
            logger.info(f"Sent GROUP_JOIN_REQUEST for '{groupname}' to admin {admin_ip}")
            await message_queue.put({"type": "log", "message": f"Sent join request for '{groupname}'."})
        except Exception as e:
             logger.error(f"Failed send GROUP_JOIN_REQUEST to {admin_ip}: {e}")
             await message_queue.put({"type": "log", "message": f"Failed send join request: {e}", "level": logging.ERROR})
    else:
        logger.warning(f"Cannot send GROUP_JOIN_REQUEST: Admin {admin_ip} not connected.")
        await message_queue.put({"type": "log", "message": f"Cannot send join request: Admin not connected.", "level": logging.WARNING})


async def send_group_join_response(groupname, requester_ip, approved):
    # This runs on the Admin's side
    own_ip = await get_own_ip() # Admin's IP
    message = json.dumps({"type": "GROUP_JOIN_RESPONSE", "groupname": groupname, "requester_ip": requester_ip, "approved": approved, "admin_ip": own_ip}) # Include admin IP
    ws = connections.get(requester_ip)
    action = "Approved" if approved else "Denied"
    if ws and ws.state == State.OPEN:
        try:
            await ws.send(message)
            logger.info(f"Sent GROUP_JOIN_RESPONSE ({action}) for '{groupname}' to {requester_ip}")
            # ADDED: Notify GUI (Admin's GUI)
            await message_queue.put({"type": "log", "message": f"{action} join request for '{groupname}'."})
            # Trigger updates locally for admin
            await message_queue.put({"type": "join_requests_update"})
            if approved: await message_queue.put({"type": "group_list_update"}) # Update member list view
        except Exception as e:
             logger.error(f"Failed send GROUP_JOIN_RESPONSE to {requester_ip}: {e}")
             await message_queue.put({"type": "log", "message": f"Failed send join response: {e}", "level": logging.ERROR})
    else:
        logger.warning(f"Cannot send GROUP_JOIN_RESPONSE: Requester {requester_ip} not connected.")
        await message_queue.put({"type": "log", "message": f"Cannot send join response: Requester not connected.", "level": logging.WARNING})
        # Remove pending request locally if peer disconnected?
        await message_queue.put({"type": "join_requests_update"})


async def send_group_update_message(groupname, members_ips_list, admin_ip=None):
    """Send group membership update to all current members."""
    # Ensure admin_ip is provided or get it from groups state
    if not admin_ip:
        async with groups_lock:
            admin_ip = groups.get(groupname, {}).get("admin")
            
    if not admin_ip:
        logger.error(f"Cannot send GROUP_UPDATE for '{groupname}': Admin IP unknown.")
        return

    message = json.dumps({"type": "GROUP_UPDATE", "groupname": groupname, "members": members_ips_list, "admin": admin_ip})
    logger.info(f"Sending GROUP_UPDATE for '{groupname}' to {len(members_ips_list)} members.")
    sent_count = 0
    
    async with connections_lock:
        for member_ip in members_ips_list:
            ws = connections.get(member_ip)
            if ws and ws.state == State.OPEN:
                try: await ws.send(message); sent_count += 1
                except Exception as e: logger.error(f"Failed send GROUP_UPDATE to {member_ip}: {e}")
            else: logger.warning(f"Cannot send GROUP_UPDATE: Member {member_ip} not connected.")
            
    logger.debug(f"Sent GROUP_UPDATE to {sent_count} members.")
    # ADDED: Trigger local GUI update as well
    await message_queue.put({"type": "group_list_update"})
