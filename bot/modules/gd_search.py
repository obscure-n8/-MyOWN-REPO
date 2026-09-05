from .. import LOGGER, user_data
from ..helper.ext_utils.bot_utils import (
    sync_to_async,
    get_telegraph_list,
    new_task,
)
from ..helper.mirror_leech_utils.gdrive_utils.search import GoogleDriveSearch
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import send_message, edit_message


from .rc_search import run_rclone_search


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value in ("True", "False"):
        return value == "True"
    return False


async def list_buttons(user_id, is_recursive=True, user_token=False):
    buttons = ButtonMaker()
    buttons.data_button(
        f"{'✅️' if user_token else '❌️'} User Token",
        f"list_types {user_id} ut {is_recursive} {user_token}",
        "header",
    )
    buttons.data_button(
        f"{'✅️' if is_recursive else '❌️'} Recursive",
        f"list_types {user_id} rec {is_recursive} {user_token}",
        "header",
    )
    buttons.data_button(
        "Folders", f"list_types {user_id} folders {is_recursive} {user_token}"
    )
    buttons.data_button(
        "Files", f"list_types {user_id} files {is_recursive} {user_token}"
    )
    buttons.data_button(
        "Both", f"list_types {user_id} both {is_recursive} {user_token}"
    )

    buttons.data_button("Cancel", f"list_types {user_id} cancel", "footer")
    return buttons.build_menu(2)


async def _list_drive(key, message, item_type, is_recursive, user_token, user_id):
    LOGGER.info(f"GD Listing: {key}")
    if user_token:
        user_dict = user_data.get(user_id, {})
        target_id = user_dict.get("GDRIVE_ID", "") or ""
        LOGGER.info(target_id)
    else:
        target_id = ""
    telegraph_content, contents_no = await sync_to_async(
        GoogleDriveSearch(is_recursive=is_recursive, item_type=item_type).drive_list,
        key,
        target_id,
        user_id,
    )
    if telegraph_content:
        try:
            button = await get_telegraph_list(telegraph_content)
        except Exception as e:
            await edit_message(message, e)
            return
        msg = f"<b>Found {contents_no} result for <i>{key}</i></b>"
        await edit_message(message, msg, button)
    else:
        await edit_message(message, f"No result found for <i>{key}</i>")


@new_task
async def select_type(_, query):
    user_id = query.from_user.id
    message = query.message
    key = message.reply_to_message.text.split(maxsplit=1)[1].strip()
    data = query.data.split()
    if user_id != int(data[1]):
        return await query.answer(text="Not Yours!", show_alert=True)
    elif data[2] == "rec":
        await query.answer()
        is_recursive = not _parse_bool(data[3])
        buttons = await list_buttons(user_id, is_recursive, _parse_bool(data[4]))
        return await edit_message(message, "Choose list options:", buttons)
    elif data[2] == "ut":
        await query.answer()
        user_token = not _parse_bool(data[4])
        buttons = await list_buttons(user_id, _parse_bool(data[3]), user_token)
        return await edit_message(message, "Choose list options:", buttons)
    elif data[2] == "cancel":
        await query.answer()
        return await edit_message(message, "<i>List has been canceled!</i>")
    await query.answer()
    item_type = data[2]
    is_recursive = _parse_bool(data[3])
    user_token = _parse_bool(data[4])
    await edit_message(message, f"<b>Searching.. for <i>{key}</i></b>")
    await _list_drive(key, message, item_type, is_recursive, user_token, user_id)


@new_task
async def select_dest(client, query):
    user_id = query.from_user.id
    message = query.message
    data = query.data.split()
    if user_id != int(data[1]):
        return await query.answer(text="Not Yours!", show_alert=True)

    dest = data[2]
    if dest == "cancel":
        await query.answer()
        return await edit_message(message, "<i>List has been canceled!</i>")

    if dest == "gdrive":
        await query.answer()
        buttons = await list_buttons(user_id)
        return await edit_message(message, "Choose list options:", buttons)

    if dest == "rclone":
        await query.answer()
        reply_to = message.reply_to_message
        if not reply_to:
            return await edit_message(message, "❌ Original message not found.")

        args = (reply_to.text or reply_to.caption or "").split()[1:]
        await run_rclone_search(client, reply_to, args, edit_message_obj=message)


@new_task
async def gdrive_search(_, message):
    if len(message.text.split()) == 1:
        return await send_message(
            message, "<i>Send a search query along with list command</i>"
        )
    user_id = message.from_user.id
    buttons = ButtonMaker()
    buttons.data_button("Google Drive", f"list_dest {user_id} gdrive")
    buttons.data_button("Rclone", f"list_dest {user_id} rclone")
    buttons.data_button("Cancel", f"list_dest {user_id} cancel")
    await send_message(message, "Choose search destination:", buttons.build_menu(2))
