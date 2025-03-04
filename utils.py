import asyncio
import base64
import importlib
import inspect
import json
import ast
import os
import re
import sys
import time
import uuid
import requests
from tenacity import retry, stop_after_attempt, wait_fixed
import aiohttp
from termcolor import colored
import memory as _memory
import openai
from fastapi import HTTPException, BackgroundTasks, UploadFile
from werkzeug.utils import secure_filename
from pathlib import Path
from chat_tabs.dao import ChatTabsDAO
from config import api_keys, default_params, fakedata, USERS_DIR
from database import Database
import tiktoken
from pydub import audio_segment
import logs
import prompts
from unidecode import unidecode
import llmcalls
from simple_utils import get_root
from user_management.dao import UsersDAO
from typing import List, Dict, Any
from datetime import datetime
import pytz
from tzlocal import get_localzone
import traceback

logger = logs.Log("utils", "utils.log").get_logger()

# Parameters for OpenAI
max_responses = 1
COT_RETRIES = {}
stopPressed = {}

MODEL_COSTS = {
    "gpt-4o": {
        "input": 0.000005,  # $5.00 / 1M tokens
        "output": 0.000015,  # $15.00 / 1M tokens
    },
    "gpt-4o-mini": {
        "input": 0.00000015,  # $0.150 / 1M tokens
        "output": 0.0000006,  # $0.600 / 1M tokens
    },
    "gpt-4-turbo": {
        "input": 0.00001,  # $10.00 / 1M tokens
        "output": 0.00003,  # $30.00 / 1M tokens
    },
    "claude-3-5-sonnet-20240620": {
        "input": 0.000003,  # $3 / 1M tokens
        "output": 0.000015,  # $15 / 1M tokens
    },
    "claude-3-opus-20240229": {
        "input": 0.000015,  # $15 / 1M tokens
        "output": 0.000075,  # $75 / 1M tokens
    },
}


class MessageSender:
    """This class contains functions to send messages to the user"""

    @staticmethod
    async def send_debug(message, number, color, username):
        """Send a debug message to the user and print it to the console with a color"""
        from importlib import import_module

        routes = import_module("routes")
        if number <= 2:
            message = f"{'-' * 50}\n{message}\n{'-' * 50}"
            new_message = {"debug": number, "color": color, "message": message}
        else:
            new_message = {"message": message}

        await routes.send_debug_message(username, json.dumps(new_message))

    @staticmethod
    async def send_message(message, color, username):
        """Send a message to the user and print it to the console with a color"""
        from importlib import import_module

        routes = import_module("routes")
        await routes.send_debug_message(username, json.dumps(message))

    @staticmethod
    async def update_token_usage(response, username, brain=False, elapsed=0):
        """Update the token usage in the database"""
        try:
            settings = await SettingsManager.load_settings("users", username)
            current_model = settings["active_model"]["active_model"]

            # if the response is audio_cost update the audio cost
            if "audio_cost" in response:
                audio_cost = response["audio_cost"]

                total_cost = 0
                # update the total cost with the whisper cost
                with Database() as db:
                    db.add_whisper_usage(username, audio_cost)
                    total_tokens_used, total_cost = db.get_token_usage(username)
                await MessageSender.send_message(
                    {
                        "audio_cost": {
                            "audio_cost": audio_cost,
                            "total_cost": total_cost,
                            "total_tokens": total_tokens_used,
                        }
                    },
                    "red",
                    username,
                )
                # we don't need to update the token usage for audio
                return

            elif hasattr(response, "usage"):
                usage = response.usage
                if hasattr(usage, "input_tokens") and hasattr(usage, "output_tokens"):
                    # Claude structure
                    total_tokens_used = usage.input_tokens + usage.output_tokens
                    input_tokens = usage.input_tokens
                    output_tokens = usage.output_tokens
                elif hasattr(usage, "prompt_tokens") and hasattr(
                    usage, "completion_tokens"
                ):
                    # OpenAI structure
                    total_tokens_used = usage.total_tokens
                    input_tokens = usage.prompt_tokens
                    output_tokens = usage.completion_tokens
                else:
                    return
            else:
                return

            with Database() as db:
                result = db.update_token_usage(
                    username,
                    total_tokens_used=total_tokens_used,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                )
                if result is not None:
                    await MessageSender.send_debug(
                        f"Last message: Input tokens: {input_tokens}, Output tokens: {output_tokens}\nTotal tokens used: {result[0]}, Input tokens: {result[1]}, Output tokens: {result[2]}",
                        2,
                        "red",
                        username,
                    )

                    # Calculate costs based on the current model
                    if current_model in MODEL_COSTS:
                        model_cost = MODEL_COSTS[current_model]
                        input_cost = round(input_tokens * model_cost["input"], 5)
                        output_cost = round(output_tokens * model_cost["output"], 5)
                    else:
                        input_cost = round(input_tokens * 0.00001, 5)
                        output_cost = round(output_tokens * 0.00003, 5)

                    this_message_total_cost = round(input_cost + output_cost, 5)

                    total_input_cost = round(result[1] * model_cost["input"], 5)
                    total_output_cost = round(result[2] * model_cost["output"], 5)
                    total_cost = round(total_input_cost + total_output_cost, 5)

                    await MessageSender.send_message(
                        {
                            "usage": {
                                "total_tokens": result[0],
                                "total_cost": total_cost,
                            }
                        },
                        "red",
                        username,
                    )

                    daily_stats = db.get_daily_stats(username)
                    if daily_stats is not None:
                        current_spending_count = daily_stats["spending_count"] or 0
                    else:
                        current_spending_count = 0
                    spending_count = current_spending_count + this_message_total_cost

                    await MessageSender.send_message(
                        {"daily_usage": {"daily_cost": spending_count}}, "red", username
                    )

                    # update the daily_stats table
                    if brain:
                        db.update_daily_stats_token_usage(
                            username,
                            brain_tokens=input_tokens,
                            spending_count=this_message_total_cost,
                        )
                    else:
                        db.update_daily_stats_token_usage(
                            username,
                            prompt_tokens=input_tokens,
                            generation_tokens=output_tokens,
                            spending_count=this_message_total_cost,
                        )

                    current_stats_spending_count = (
                        db.get_statistic(username)["total_spending_count"] or 0
                    )
                    spending_count = round(
                        current_stats_spending_count + this_message_total_cost, 5
                    )
                    # update the statistics table
                    db.update_statistic(username, total_spending_count=spending_count)

                    # update the elapsed time
                    # get the total response time and response count from the database
                    stats = db.get_daily_stats(username)
                    if stats is not None:
                        total_response_time = stats["total_response_time"] or 0
                        response_count = stats["response_count"] or 0
                    else:
                        total_response_time = 0
                        response_count = 0
                    # update the total response time and response count
                    total_response_time += elapsed
                    response_count += 1

                    # calculate the new average response time
                    average_response_time = total_response_time / response_count

                    db.replace_daily_stats_token_usage(
                        username,
                        total_response_time=total_response_time,
                        average_response_time=average_response_time,
                        response_count=response_count,
                    )

        except Exception as e:
            logger.exception(f"An error occurred (utils): {e}")
            await MessageSender.send_message(
                {"error": "An error occurred (utils): " + str(e)}, "red", username
            )


class AddonManager:
    """This class contains functions to load addons"""

    @staticmethod
    async def load_addons(username, users_dir):
        anthropic_api_key = api_keys.get("anthropic")
        openai_api_key = api_keys.get("openai")
        if anthropic_api_key:
            active_model = "claude-3-opus-20240229"
        elif openai_api_key:
            active_model = "gpt-4o"
        else:
            active_model = "gpt-4o"
        default_settings = {
            "addons": {},
            "audio": {"voice_input": True, "voice_output": True},
            "avatar": {"avatar": False},
            "language": {"language": "en"},
            "system_prompt": {"system_prompt": "stoic"},
            "cot_enabled": {"cot_enabled": False},
            "verbose": {"verbose": False},
            "timezone": {"timezone": "Auto"},
            "active_model": {"active_model": active_model},
            "memory": {
                "functions": 6400,
                "ltm1": 2560,
                "ltm2": 2560,
                "episodic": 2560,
                "recent": 2560,
                "notes": 2560,
                "input": 104960,
                "output": 3840,
                "max_tokens": 128000,
                "min_tokens": 500,
            },
        }

        data_username = convert_username(username)

        function_dict = {}
        function_metadata = []
        module_timestamps = {}

        data_dir = os.path.join(users_dir, data_username, "data")
        # create the users directory if it doesn't exist
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)

        user_path = os.path.join(users_dir, username)
        settings_file = os.path.join(user_path, "settings.json")

        if not os.path.exists(user_path):
            os.makedirs(user_path)

        if not os.path.exists(settings_file):
            with open(settings_file, "w") as f:
                json.dump(default_settings, f)

        # Check if settings file exists and is not empty
        if os.path.exists(settings_file) and os.path.getsize(settings_file) > 0:
            try:
                with open(settings_file, "r") as f:
                    settings = json.load(f)
            except json.JSONDecodeError:
                settings = default_settings
        else:
            settings = default_settings

        # Validate and correct the settings
        for key, default_value in default_settings.items():
            # If the key is not in the settings or the type is not the same as the default value, set it to the default value
            if key not in settings or type(settings[key]) != type(default_value):
                settings[key] = default_value
            # If the key is a dict, validate and correct the sub keys
            elif isinstance(default_value, dict):
                for sub_key, sub_default_value in default_value.items():
                    if sub_key not in settings[key] or type(
                        settings[key][sub_key]
                    ) != type(sub_default_value):
                        settings[key][sub_key] = sub_default_value

        # Save the settings
        with open(settings_file, "w") as f:
            json.dump(settings, f)

        # Delete addons that don't exist anymore
        for addon in list(settings["addons"].keys()):
            if not os.path.exists(os.path.join("addons", f"{addon}.py")):
                del settings["addons"][addon]

        # Load addons
        for filename in os.listdir("addons"):
            if filename.endswith(".py"):
                addon_name = filename[:-3]
                if addon_name not in settings["addons"]:
                    settings["addons"][addon_name] = False

                # Only load the addon if it is enabled
                if settings["addons"].get(addon_name, True):
                    file_path = os.path.join("addons", filename)
                    spec = importlib.util.spec_from_file_location(
                        filename[:-3], file_path
                    )
                    module = importlib.util.module_from_spec(spec)

                    # Check if the module has been modified since it was last imported
                    file_timestamp = os.path.getmtime(file_path)
                    if (
                        filename in module_timestamps
                        and file_timestamp > module_timestamps[filename]
                    ):
                        module = importlib.reload(module)
                    module_timestamps[filename] = file_timestamp

                    spec.loader.exec_module(module)

                    # Check for the function in the module and add it with the required structure
                    if module.__name__ in module.__dict__:
                        function_detail = {
                            "name": module.__name__,
                            "description": getattr(
                                module, "description", "No description"
                            ),
                            "parameters": getattr(
                                module, "parameters", "No parameters"
                            ),
                        }
                        # Wrap the function detail in the required structure
                        tool_metadata = {
                            "type": "function",
                            "function": function_detail,
                        }
                        function_metadata.append(tool_metadata)
                    else:
                        await MessageSender.send_debug(
                            f"Module {module.__name__} does not have a function with the same name.",
                            2,
                            "red",
                            username,
                        )

        with open(settings_file, "w") as f:
            json.dump(settings, f)

        if not function_metadata:
            function_metadata = fakedata

        return function_dict, function_metadata


class AudioProcessor:
    """This class contains functions to process audio"""

    @staticmethod
    async def upload_audio(user_dir, username, audio_file: UploadFile):
        # save the file to the user's folder
        userdir = os.path.join(user_dir, username)
        filename = secure_filename(audio_file.filename)
        audio_dir = os.path.join(userdir, "audio")
        if not os.path.exists(audio_dir):
            os.makedirs(audio_dir)
        filepath = os.path.join(audio_dir, filename)
        with open(filepath, "wb") as f:
            f.write(audio_file.file.read())

        # check the audio for voice
        if not AudioProcessor.check_voice(filepath):
            return {"transcription": ""}

        # get the user settings language
        settings_file = os.path.join(userdir, "settings.json")
        with open(settings_file, "r") as f:
            settings = json.load(f)
        language = settings.get("language", {}).get("language", "en")

        # if no language is set, default to english
        if language is None:
            language = "en"

        # use the saved file path to transcribe the audio
        transcription = await AudioProcessor.transcribe_audio(
            filepath, language, username
        )
        return {"transcription": transcription}

    @staticmethod
    def check_voice(filepath):
        audio = audio_segment.AudioSegment.from_file(filepath)
        if (
            audio.dBFS < -40
        ):  #  Decrease for quieter voices (-50), increase for louder voices (-20)
            logger.info("No voice detected, dBFS: " + str(audio.dBFS))
            return False
        return True

    @staticmethod
    async def transcribe_audio(audio_file_path, language, username=""):
        # Load the audio file using pydub
        audio = audio_segment.AudioSegment.from_file(audio_file_path)

        # Calculate the duration of the audio in seconds
        duration_seconds = len(audio) / 1000

        # Calculate the cost of transcription
        cost_per_minute = 0.006
        cost = (duration_seconds / 60) * cost_per_minute

        openai_response = llmcalls.OpenAIResponser(api_keys["openai"], default_params)
        transcription = await openai_response.get_audio_transcription(
            audio_file_path, language, "audio"
        )
        # Adding this to voice usage for now
        with Database() as db:
            db.add_whisper_usage(username, cost)

        return transcription.text

    @staticmethod
    async def generate_audio(text, username, users_dir):
        openai_response = llmcalls.OpenAIResponser(api_keys["openai"], default_params)
        # return an error if the text has more than 4096 characters (openai limit for now)
        if len(text) > 4096:
            return {"error": "The text is too long."}
        audio_path = await openai_response.generate_audio(text, username, users_dir)
        return audio_path


class BrainProcessor:
    """This class contains functions to process the brain *DEPRECATED*"""

    @staticmethod
    async def delete_recent_messages(user):
        print(f"Deleting recent messages for {user}")


class MessageParser:
    """This class contains functions to parse messages and generate responses"""

    @staticmethod
    def strip_code_blocks(text):
        # Function to strip code blocks from text
        code_blocks = []
        code_block_start = 0
        code_block_end = 0
        for i in range(len(text)):
            if text[i : i + 3] == "```":
                if code_block_start == 0:
                    code_block_start = i
                else:
                    code_block_end = i
                    code_blocks.append(text[code_block_start : code_block_end + 3])
                    code_block_start = 0
                    code_block_end = 0
        for code_block in code_blocks:
            text = text.replace(code_block, "")
        return text

    @staticmethod
    async def start_image_description(image_path, prompt, file_name, username):
        """Get the description of an image using the OpenAI Vision API, asynchronously."""
        openai_response = llmcalls.OpenAIResponser(api_keys["openai"], default_params)
        resp = await openai_response.get_image_description(
            image_path=image_path, prompt=prompt, filename=file_name, username=username
        )
        result = prompts.image_description.format(prompt, file_name, resp)
        return result

    @staticmethod
    def add_file_paths_to_message(message, file_paths):
        """Add file paths to the message"""
        new_message = file_paths + "\n" + message
        return new_message

    @staticmethod
    async def get_recent_messages(
        username: str, chat_id: str, regenerator: bool = False, uuid: str = None
    ) -> List[Dict[str, Any]]:
        memory = _memory.MemoryManager()
        settings = await SettingsManager.load_settings("users", username)
        memory.model_used = settings["active_model"]["active_model"]
        recent_messages = await memory.get_most_recent_messages(
            "active_brain", username, chat_id=chat_id
        )

        if regenerator and uuid:
            # Initialize a new list to hold the filtered messages
            filtered_messages = []
            # Iterate over the messages to find the message with the matching UUID
            for message in recent_messages:
                # Add the current message to the filtered list
                filtered_messages.append(message)
                # If the current message's UUID matches the specified UUID, stop adding messages
                if message["metadata"]["uid"] == uuid:
                    # remove the last message too
                    filtered_messages.pop()
                    break
            # Use the filtered list for the rest of the function
            recent_messages = filtered_messages

        return [
            {
                "document": message["document"],
                "metadata": message["metadata"],
                "id": message["id"],
            }
            for message in recent_messages
        ]

    @staticmethod
    def get_message(type, parameters):
        with UsersDAO() as db:
            display_name = db.get_display_name(parameters["username"])
        """Parse the message based on the type and parameters"""
        if type == "start_message":
            return prompts.start_message.format(
                display_name, parameters["memory"], parameters["instruction_string"]
            )
        elif type == "keyword_generation":
            return prompts.keyword_generation.format(
                display_name,
                parameters["memory"],
                parameters["last_messages_string"],
                parameters["message"],
            )

    @staticmethod
    async def convert_function_call_arguments(
        arguments, username, tryAgain=True, chat_id=None
    ):
        # if arguments is a list, convert it to a dictionary
        if isinstance(arguments, list):
            # take the first element of the list for now, dirty fix
            arguments = arguments[0] if arguments else {}
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
        except json.JSONDecodeError:
            try:
                arguments = ast.literal_eval(arguments)
            except (ValueError, SyntaxError):
                try:
                    arguments = re.sub(r"```.*?\n", "", arguments)
                    arguments = re.sub(r"```", "", arguments)
                    arguments = re.sub(r"\.\.\.|\…", "", arguments)
                    arguments = re.sub(r"[\r\n]+", "", arguments)
                    arguments = re.sub(r"[^\x00-\x7F]+", "", arguments)
                    arguments = json.loads(arguments)
                except Exception:
                    try:
                        arguments = eval(arguments)
                    except Exception:
                        if tryAgain:
                            # ask openai to re-generate the message
                            message = prompts.invalid_json.format(arguments)
                            logger.exception(
                                f"Invalid json: {arguments}, username: {username}, trying to re-generate the message..."
                            )
                            messages = [
                                {
                                    "role": "system",
                                    "content": prompts.invalid_json_system_prompt,
                                },
                                {"role": "user", "content": f"{message}"},
                            ]
                            response = None
                            settings = await SettingsManager.load_settings(
                                "users", username
                            )

                            default_par = default_params
                            default_par["max_tokens"] = settings.get("memory", {}).get(
                                "output", 1000
                            )
                            default_par["model"] = settings.get("active_model").get(
                                "active_model"
                            )
                            responder = llmcalls.get_responder(
                                (
                                    api_keys["openai"]
                                    if settings.get("active_model")
                                    .get("active_model")
                                    .startswith("gpt")
                                    else api_keys["anthropic"]
                                ),
                                settings.get("active_model").get("active_model"),
                                default_par,
                            )

                            async for resp in responder.get_response(
                                username,
                                messages,
                                stream=False,
                                function_metadata=fakedata,
                                function_call="auto",
                                chat_id=chat_id,
                            ):
                                response = resp
                            await MessageParser.convert_function_call_arguments(
                                response,
                                username,
                                False,
                                chat_id=chat_id,
                            )
                        else:
                            logger.exception(
                                f"Invalid json: {arguments}, username: {username}"
                            )
                            return None
        return arguments if isinstance(arguments, dict) else {}

    @staticmethod
    def handle_function_response(function, args):
        try:
            function_response = function(**args)
        except Exception as e:
            logger.error(f"Error: {e}")
            function_response = {"content": "error: " + str(e)}
        return function_response

    @staticmethod
    async def ahandle_function_response(function, args):
        try:
            function_response = await function(**args)
        except Exception as e:
            logger.error(f"Error: {e}")
            function_response = {"content": "error: " + str(e)}
        return function_response

    @staticmethod
    async def process_function_call(
        function_call_name,
        function_call_arguments,
        function_dict,
        function_metadata,
        message,
        og_message,
        username,
        exception_handler,
        merge=True,
        users_dir=USERS_DIR,
        steps_string="",
        full_response=None,
        chat_id=None,
    ):
        converted_function_call_arguments = (
            await MessageParser.convert_function_call_arguments(
                function_call_arguments, username, chat_id=chat_id
            )
        )

        # add the username to the arguments
        if converted_function_call_arguments is not None:
            converted_function_call_arguments["username"] = username
        else:
            logger.error(
                "converted_function_call_arguments is None: "
                + str(function_call_arguments)
            )
            return {
                "content": "error: converted_function_call_arguments is None: "
                + str(function_call_arguments)
            }
        new_message = {
            "functioncall": "yes",
            "color": "red",
            "message": {
                "function": function_call_name,
                "arguments": function_call_arguments,
            },
            "chat_id": chat_id,
        }
        await MessageSender.send_message(new_message, "red", username)
        try:
            # Print the available functions for debugging
            # print(f"Available functions: {list(function_dict.keys())}")

            # Try to get the module corresponding to the function_call_name
            module = function_dict.get(function_call_name)
            if module:
                try:
                    # Attempt to retrieve the callable function from the module
                    actual_function = getattr(module, function_call_name)
                    if actual_function and callable(actual_function):
                        # print(f"Executing {function_call_name}...")
                        if asyncio.iscoroutinefunction(actual_function):
                            function_response = (
                                await MessageParser.ahandle_function_response(
                                    actual_function, converted_function_call_arguments
                                )
                            )
                        else:
                            function_response = MessageParser.handle_function_response(
                                actual_function, converted_function_call_arguments
                            )
                            confirm_email_responses = [
                                response
                                for response in function_response
                                if "action" in response
                                and response["action"] == "confirm_email"
                            ]

                            if confirm_email_responses:
                                await MessageSender.send_message(
                                    {
                                        "type": "confirm_email",
                                        "content": confirm_email_responses,
                                    },
                                    "blue",
                                    username,
                                )
                                # add a message to the response
                                function_response.append(
                                    {
                                        "note": "\nThe email has been prepared for the user. Ask the user to send or confirm the email themselves."
                                    }
                                )
                    else:
                        print(
                            f"Function {function_call_name} not found within the module."
                        )
                        function_response = f"Function {function_call_name} not found."
                except AttributeError as e:
                    print(
                        f"Error accessing function {function_call_name} in module: {e}"
                    )
                    function_response = (
                        f"Error accessing function {function_call_name}: {e}"
                    )
            else:
                print(f"Module for {function_call_name} not found.")
                function_response = f"Module for {function_call_name} not found."
        except Exception as e:
            print(f"Error: {e}")
            function_response = f"Error: {e}"
        return await process_function_reply(
            function_call_name,
            function_response,
            message,
            og_message,
            function_dict,
            function_metadata,
            username,
            merge,
            users_dir,
            chat_id,
        )

    @staticmethod
    def extract_content(data):
        try:
            response = json.loads(data)
        except:
            return data
        if isinstance(response, dict):
            if "content" in response:
                return MessageParser.extract_content(response["content"])
        return response

    @staticmethod
    async def process_response(
        response,
        function_dict,
        function_metadata,
        message,
        og_message,
        username,
        process_message,
        chat_history,
        chat_metadata,
        history_ids,
        history_string,
        last_messages_string,
        old_kw_brain,
        users_dir,
        chat_id,
        regenerate=False,
        uuid=None,
    ):
        if "function_call" in response:
            function_call_name = response["function_call"]["name"]
            function_call_arguments = response["function_call"]["arguments"]
            response = await MessageParser.process_function_call(
                function_call_name,
                function_call_arguments,
                function_dict,
                function_metadata,
                message,
                og_message,
                username,
                process_message,
                False,
                "users/",
                "",
                None,
                chat_id=chat_id,
            )
        else:
            await MessageSender.send_debug(f"{message}", 1, "green", username)
            await MessageSender.send_debug(
                f"Assistant: {response['content']}", 1, "pink", username
            )
            if response["content"] is not None:
                with UsersDAO() as db:
                    display_name = db.get_display_name(username)
                memory = _memory.MemoryManager()
                settings = await SettingsManager.load_settings("active_brain", username)
                memory.model_used = settings["active_model"]["active_model"]
                await memory.process_incoming_memory_assistant(
                    "active_brain",
                    response["content"],
                    username,
                    chat_id=chat_id,
                    regenerate=regenerate,
                    uid=uuid,
                )
        return response

    @staticmethod
    def num_tokens_from_string(string, model="gpt-4"):
        """Returns the number of tokens in a text string."""
        try:
            encoding = tiktoken.encoding_for_model(model)
        except (KeyError, ValueError):
            logger.warning("Warning: model not found. Using cl100k_base encoding.")
            # encoding = tiktoken.get_encoding("cl100k_base")
            return len(string)
        num_tokens = len(encoding.encode(string))
        return num_tokens

    @staticmethod
    def num_tokens_from_functions(functions, model="gpt-4"):
        """Return the number of tokens used by a list of functions."""
        try:
            encoding = tiktoken.encoding_for_model(model)
        except (KeyError, ValueError):
            logger.warning("Warning: model not found. Using cl100k_base encoding.")
            return 0

        num_tokens = 0
        for tool in functions:  # Iterate over the tools
            if tool["type"] == "function":  # Ensure it's a function type
                function = tool["function"]  # Extract the function details
                function_tokens = len(encoding.encode(function["name"]))
                function_tokens += len(encoding.encode(function["description"]))

                if "parameters" in function:
                    parameters = function["parameters"]
                    if "properties" in parameters:
                        for propertiesKey, v in parameters["properties"].items():
                            function_tokens += len(encoding.encode(propertiesKey))
                            for field in v:
                                if field == "type":
                                    function_tokens += 2
                                    function_tokens += len(encoding.encode(v["type"]))
                                elif field == "description":
                                    function_tokens += 2
                                    function_tokens += len(
                                        encoding.encode(v["description"])
                                    )
                                elif field == "default":
                                    function_tokens += 2
                                elif field == "enum":
                                    function_tokens -= 3
                                    for o in v["enum"]:
                                        function_tokens += 3
                                        function_tokens += len(encoding.encode(o))
                                elif field == "items":
                                    function_tokens += 10
                                else:
                                    logger.warning(
                                        f"Warning: not supported field {field}"
                                    )
                        function_tokens += 11

                num_tokens += function_tokens

        num_tokens += 12  # Account for any additional overhead
        return num_tokens

    async def generate_full_message(username, merged_result_string, instruction_string):
        full_message = MessageParser.get_message(
            "start_message",
            {
                "username": username,
                "memory": merged_result_string,
                "instruction_string": instruction_string,
            },
        )
        return full_message


class SettingsManager:
    """This class contains functions to load settings"""

    @staticmethod
    def get_user_dir():
        # get the CHARLIE_USER_DIR
        full_path = os.getenv("CHARLIE_USER_DIR")
        if full_path is None:
            full_path = USERS_DIR
        return full_path

    @staticmethod
    def get_version():
        version = "0.0"
        with open(get_root("version.txt"), "r") as f:
            new_version = f.read().strip()
            if new_version:
                version = new_version
        return version

    @staticmethod
    async def load_settings(users_dir, username):
        settings = {}
        settings_dir = os.path.join(users_dir, username)
        settings_file = os.path.join(settings_dir, "settings.json")

        # Make sure the directory exists
        if not os.path.exists(settings_dir):
            os.makedirs(settings_dir)

        # Make sure the settings file exists
        if not os.path.exists(settings_file):
            with open(settings_file, "w") as f:
                # Write default settings to the file
                json.dump({}, f)

        # Load addons
        await AddonManager.load_addons(username, users_dir)

        # Load settings
        with open(settings_file, "r") as f:
            settings = json.load(f)

        # Update memory settings to the new format if needed
        if "memory" in settings:
            memory_settings = settings["memory"]
            if isinstance(memory_settings.get("ltm1"), float):
                max_tokens = memory_settings.get("max_tokens", 8000)
                memory_settings["functions"] = int(
                    memory_settings.get("functions", 0.05) * max_tokens
                )
                memory_settings["ltm1"] = int(
                    memory_settings.get("ltm1", 0.02) * max_tokens
                )
                memory_settings["ltm2"] = int(
                    memory_settings.get("ltm2", 0.02) * max_tokens
                )
                memory_settings["episodic"] = int(
                    memory_settings.get("episodic", 0.02) * max_tokens
                )
                memory_settings["recent"] = int(
                    memory_settings.get("recent", 0.02) * max_tokens
                )
                memory_settings["notes"] = int(
                    memory_settings.get("notes", 0.02) * max_tokens
                )
                memory_settings["input"] = int(
                    memory_settings.get("input", 0.82) * max_tokens
                )
                memory_settings["output"] = int(
                    memory_settings.get("output", 0.03) * max_tokens
                )

        return settings

    @staticmethod
    async def get_current_date_time(username):
        settings = await SettingsManager().load_settings("users", username)
        timezone = settings.get("timezone", {}).get("timezone", "auto")

        if timezone == "auto" or timezone == "Auto":
            # If the timezone is set to 'auto', use the local timezone
            user_tz = get_localzone()
        else:
            # Convert the timezone from 'UTC+X' format to a valid pytz timezone name
            if timezone.startswith("UTC"):
                offset = timezone[3:]
                if offset.startswith("+"):
                    offset = offset[1:]
                offset_hours = float(offset)
                user_tz = pytz.FixedOffset(offset_hours * 60)
            else:
                user_tz = pytz.timezone(timezone)

        current_date_time = datetime.now(user_tz).isoformat()
        return current_date_time


def needsTabDescription(chat_id):
    # get the tab description for the chat
    with Database() as db:
        tab_description = db.get_tab_description(chat_id)
        if tab_description.startswith("New Chat"):
            return True
        else:
            return False


async def process_message(
    og_message,
    username,
    users_dir,
    display_name=None,
    image_prompt=None,
    chat_id=None,
    regenerate=False,
    uuid=None,
    timestamp=None,
):
    """Process the message and generate a response"""
    # reset the stopPressed variable
    llmcalls.reset_stop_stream(username)
    if display_name is None:
        display_name = username

    # load the setting for the user
    settings = await SettingsManager.load_settings(users_dir, username)

    # Retrieve memory settings
    memory_settings = settings.get("memory", {})
    # start prompt = 54 tokens + 200 reserved for an image description + 23 for the notes string
    token_usage = 500
    # Extract individual settings with defaults if not found
    max_token_usage = min(memory_settings.get("max_tokens", 8000), 128000)
    remaining_tokens = max_token_usage - token_usage

    # Calculate token allocations based on percentages
    tokens_active_brain = int(memory_settings.get("ltm1", 500))
    tokens_cat_brain = int(memory_settings.get("ltm2", 500))
    tokens_episodic_memory = int(memory_settings.get("episodic", 250))
    tokens_recent_messages = int(memory_settings.get("recent", 1000))
    tokens_notes = int(memory_settings.get("notes", 750))
    tokens_input = int(memory_settings.get("input", 1000))
    tokens_output = min(int(memory_settings.get("output", 4000)), 4000)

    chat_history, chat_metadata, history_ids = [], [], []
    function_dict, function_metadata = await AddonManager.load_addons(
        username, users_dir
    )

    function_call_token_usage = MessageParser.num_tokens_from_functions(
        function_metadata, "gpt-4"
    )
    logger.debug(f"function_call_token_usage: {function_call_token_usage}")
    token_usage += function_call_token_usage
    remaining_tokens -= function_call_token_usage

    last_messages = await MessageParser.get_recent_messages(
        username, chat_id, regenerate, uuid
    )

    # for message in last_messages:
    #     print(
    #         f"{message['metadata'].get('username')}: {message['document']}",
    #     )
    last_messages_string = "\n".join(
        f"{message['metadata'].get('username')}: {message['document']}"
        for message in last_messages[-100:]
    )

    # keep the last messages below 1000 tokens, if it is above 1000 tokens, delete the oldest messages until it is below 1000 tokens
    last_messages_tokens = MessageParser.num_tokens_from_string(
        last_messages_string, "gpt-4"
    )
    if tokens_recent_messages > 100:
        logger.debug(
            f"last_messages_tokens: {last_messages_tokens} count: {len(last_messages)}"
        )
        while last_messages_tokens > tokens_recent_messages:
            last_messages.pop(0)
            last_messages_string = "\n".join(
                f"{message['metadata'].get('username')}: {message['document']}"
                for message in last_messages[-100:]
            )
            last_messages_tokens = MessageParser.num_tokens_from_string(
                last_messages_string, "gpt-4"
            )
            logger.debug(
                f"new last_messages_tokens: {last_messages_tokens} count: {len(last_messages)}"
            )
    else:
        last_messages_string = ""

    all_messages = (
        last_messages_string
        + "\n"
        + og_message
        + "\n Do not reply to any of the previous questions or messages! The chat above is for reference only. It is important to follow the previously given instructions and adhere to the required format and structure. Do not say anything else.\n"
    )

    with ChatTabsDAO() as dao:
        needs_tab_description = dao.needs_tab_description(chat_id)
    if needs_tab_description and len(last_messages) >= 2:
        response = None
        messages = [
            {
                "role": "system",
                "content": "You are a chat tab title generator. You will give a very short description of the given conversation, keep it under 5 words. Do not answer the conversation, only give a title to it! Only reply with the title, nothing else!",
            },
            {"role": "user", "content": all_messages},
        ]
        response = "New Chat"
        default_par = default_params
        default_par["max_tokens"] = settings.get("memory", {}).get("output", 1000)
        default_par["model"] = settings.get("active_model").get("active_model")
        responder = llmcalls.get_responder(
            (
                api_keys["openai"]
                if settings.get("active_model").get("active_model").startswith("gpt")
                else api_keys["anthropic"]
            ),
            settings.get("active_model").get("active_model"),
            default_par,
        )

        async for resp in responder.get_response(
            username,
            messages,
            stream=False,
            function_metadata=fakedata,
            chat_id=chat_id,
        ):
            response = resp

        with ChatTabsDAO() as db:
            db.update_tab_description(chat_id, response)

        await MessageSender.send_message(
            {
                "tab_id": chat_id,
                "tab_description": response,
            },
            "blue",
            username,
        )

    message_tokens = MessageParser.num_tokens_from_string(all_messages, "gpt-4")
    token_usage += message_tokens
    remaining_tokens -= message_tokens
    logger.debug(f"1. remaining_tokens: {remaining_tokens}")
    verbose = settings.get("verbose").get("verbose")

    if regenerate is False:
        memory = _memory.MemoryManager()
        memory.model_used = settings["active_model"]["active_model"]
        # Use the provided timestamp instead of the current time
        custom_metadata = {"created_at": timestamp.timestamp()} if timestamp else {}
        (
            kw_brain_string,
            token_usage_active_brain,
            unique_results1,
        ) = await memory.process_active_brain(
            og_message,
            username,
            all_messages,
            tokens_active_brain,
            verbose,
            chat_id=chat_id,
            regenerate=regenerate,
            uid=uuid,
            settings=settings,
            custom_metadata=custom_metadata,
        )

        token_usage += token_usage_active_brain
        remaining_tokens -= token_usage_active_brain
        logger.debug(f"2. remaining_tokens: {remaining_tokens}")

        if tokens_episodic_memory > 100:
            episodic_memory, timezone = await memory.process_episodic_memory(
                og_message,
                username,
                all_messages,
                tokens_episodic_memory,
                verbose,
                settings,
            )
            if (
                episodic_memory is None
                or episodic_memory == ""
                or episodic_memory == "none"
            ):
                episodic_memory_string = ""
            else:
                episodic_memory_string = (
                    f"""Episodic Memory of {timezone}:\n{episodic_memory}\n"""
                )
        else:
            episodic_memory_string = ""
        episodic_memory_tokens = MessageParser.num_tokens_from_string(
            episodic_memory_string, "gpt-4"
        )
        token_usage += episodic_memory_tokens
        remaining_tokens -= episodic_memory_tokens
        logger.debug(f"3. remaining_tokens: {remaining_tokens}")

        if tokens_cat_brain > 100:
            (
                results,
                token_usage_relevant_memory,
                unique_results2,
            ) = await memory.process_incoming_memory(
                None, og_message, username, tokens_cat_brain, verbose, settings
            )
            merged_results_dict = {
                id: (document, distance, formatted_date)
                for id, document, distance, formatted_date in unique_results1.union(
                    unique_results2
                )
            }
            merged_results_list = [
                (id, document, distance, formatted_date)
                for id, (
                    document,
                    distance,
                    formatted_date,
                ) in merged_results_dict.items()
            ]
            merged_results_list.sort(key=lambda x: int(x[0]))

            # Create the result string
            merged_result_string = "\n".join(
                f"({id}){formatted_date} - {document} (score: {distance})"
                for id, document, distance, formatted_date in merged_results_list
            )
        else:
            results = ""
            token_usage_relevant_memory = 0
            merged_result_string = ""

        token_usage += token_usage_relevant_memory
        remaining_tokens -= token_usage_relevant_memory
        logger.debug(f"4. remaining_tokens: {remaining_tokens}")

        # process the results
        history_string = results

        observations = "No observations available."
        instruction_string = (
            f"""{episodic_memory_string}\nObservations:\n{observations}\n"""
        )

        if tokens_notes > 100:
            notes = await memory.note_taking(
                content=all_messages,
                message=og_message,
                user_dir=users_dir,
                username=username,
                show=False,
                verbose=verbose,
                tokens_notes=tokens_notes,
                settings=settings,
            )

            if notes is not None or notes != "":
                notes_string = prompts.notes_string.format(notes)
                instruction_string += notes_string
        else:
            notes = ""
            notes_string = ""

        notes_tokens = MessageParser.num_tokens_from_string(notes_string, "gpt-4")
        token_usage += notes_tokens
        remaining_tokens -= notes_tokens
        logger.debug(f"5. remaining_tokens: {remaining_tokens}")

    if image_prompt is not None:
        image_prompt_injection = (
            "Automatically generated image description:\n" + image_prompt
        )
        og_message += "\n" + image_prompt_injection

    full_message = await MessageParser.generate_full_message(
        username, merged_result_string, instruction_string
    )

    history_messages = []

    for message in last_messages:
        role = "assistant" if message["metadata"]["username"] == "assistant" else "user"
        content = message["document"]
        history_messages.append({"role": role, "content": content})

    await MessageSender.send_debug(f"[{full_message}]", 1, "gray", username)

    cot_settings = settings.get("cot_enabled")
    is_cot_enabled = cot_settings.get("cot_enabled")
    logger.debug(f"is_cot_enabled: {is_cot_enabled}")

    if remaining_tokens < 0:
        # something went wrong in the calculations, return an error
        print(f"remaining_tokens: {remaining_tokens}, token_usage: {token_usage}")
        print(
            f"message_tokens: {message_tokens}, function_call_token_usage: {function_call_token_usage}, token_usage_active_brain: {token_usage_active_brain}, episodic_memory_tokens: {episodic_memory_tokens}, token_usage_relevant_memory: {token_usage_relevant_memory}, notes_tokens: {notes_tokens}"
        )
        print(
            f"user settings:\ntokens_active_brain: {tokens_active_brain}, tokens_cat_brain: {tokens_cat_brain}, tokens_episodic_memory: {tokens_episodic_memory}, tokens_recent_messages: {tokens_recent_messages}, tokens_notes: {tokens_notes}, tokens_input: {tokens_input}, tokens_output: {tokens_output}, max_token_usage: {max_token_usage}"
        )
        raise HTTPException(
            status_code=400,
            detail="Token limit exceeded. Please reduce the length of your message or adjust memory settings.",
        )

    response = await generate_response(
        full_message,
        og_message,
        username,
        users_dir,
        tokens_output,
        history_messages,
        chat_id=chat_id,
        regenerate=regenerate,
        uid=uuid,
        timestamp=timestamp,
    )
    response = MessageParser.extract_content(response)
    response = json.dumps({"content": response}, ensure_ascii=False)
    response = json.loads(response)

    # process the response
    response = await MessageParser.process_response(
        response,
        function_dict,
        function_metadata,
        og_message,
        og_message,
        username,
        process_message,
        chat_history,
        chat_metadata,
        history_ids,
        history_string,
        last_messages_string,
        kw_brain_string,
        users_dir,
        chat_id=chat_id,
        regenerate=regenerate,
        uuid=uuid,
    )

    with Database() as db:
        db.update_message_count(username)
    return response


def prettyprint(msg, color):
    print(colored(msg, color))


async def generate_response(
    memory_message,
    og_message,
    username,
    users_dir,
    max_allowed_tokens,
    history_messages,
    chat_id=None,
    regenerate=False,
    uid=None,
    timestamp=None,
):
    function_dict, function_metadata = await AddonManager.load_addons(
        username, users_dir
    )

    # get the system prompt settings
    settings = await SettingsManager.load_settings(users_dir, username)
    settings_system_prompt = settings.get("system_prompt").get("system_prompt")
    system_prompt = prompts.system_prompt

    if settings_system_prompt == "None":
        system_prompt = prompts.system_prompt
    elif settings_system_prompt == "stoic":
        system_prompt = prompts.stoic_system_prompt + "\n" + prompts.system_prompt
    else:
        system_prompt = settings_system_prompt + "\n" + prompts.system_prompt

    # Modify the system prompt to include the custom timestamp
    if timestamp:
        custom_time = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        system_prompt = f"Current date: {custom_time}\n" + system_prompt
    else:
        current_date_time = await SettingsManager.get_current_date_time(username)
        system_prompt = current_date_time + "\n" + system_prompt

    messages = [
        {"role": "system", "content": system_prompt + "\n" + memory_message},
    ]

    for message in history_messages:
        messages.append(
            {"role": f"{message['role']}", "content": f"{message['content']}"}
        )
    messages.append({"role": "user", "content": f"{og_message}"})

    # # print the message in a different color in the terminal
    # for message in messages:
    #     if message["role"] == "user":
    #         prettyprint(f"User: {message['content']}", "blue")
    #     elif message["role"] == "assistant":
    #         prettyprint(f"Assistant: {message['content']}", "yellow")
    #     else:
    #         prettyprint(f"System: {message['content']}", "green")

    response = ""
    default_par = default_params
    default_par["max_tokens"] = settings.get("memory", {}).get("output", 1000)
    default_par["model"] = settings.get("active_model").get("active_model")
    responder = llmcalls.get_responder(
        (
            api_keys["openai"]
            if settings.get("active_model").get("active_model").startswith("gpt")
            else api_keys["anthropic"]
        ),
        settings.get("active_model").get("active_model"),
        default_par,
    )

    async for resp in responder.get_response(
        username,
        messages,
        stream=True,
        function_metadata=function_metadata,
        chat_id=chat_id,
        uid=uid,
    ):
        response = resp
        await MessageSender.send_debug(f"response: {response}", 1, "green", username)

    return response


async def process_execute_python(
    function_response,
    message,
    username,
    chat_id=None,
    full_response=None,
):
    settings = await SettingsManager.load_settings("users", username)
    default_par = default_params
    default_par["max_tokens"] = settings.get("memory", {}).get("output", 1000)
    default_par["model"] = settings.get("active_model").get("active_model")
    responder = llmcalls.get_responder(
        (
            api_keys["openai"]
            if settings.get("active_model").get("active_model").startswith("gpt")
            else api_keys["anthropic"]
        ),
        settings.get("active_model").get("active_model"),
        default_par,
    )
    joined_message = "".join(message)
    messages = [
        {
            "role": "user",
            "content": f'This is an automated response:\nPrevious messages: {full_response}\nYou executed this: {joined_message}\nwith the following result: {function_response}\nUse this info to continue the conversation as if you executed the code and can see the output, do not mention I gave this information or say "Thank you for providing.." or similar. If files are generated, add a link in markdown format, for example [description](data/filename.ext) or html tags for videos (without triple quotes). Do not use sandbox:/ or similar prefixes for file paths.',
        },
    ]
    # debug print the message, properly formatted
    # for message in messages:
    #     if message["role"] == "user":
    #         prettyprint(f"User: {message['content']}", "blue")
    #     elif message["role"] == "assistant":
    #         prettyprint(f"Assistant: {message['content']}", "yellow")
    #     else:
    #         prettyprint(f"System: {message['content']}", "green")

    async for resp in responder.get_response(
        username,
        messages,
        stream=False,
        chat_id=chat_id,
        function_metadata=fakedata,
        function_call="none",
    ):
        # print(f"function response (utils): {resp}\ntrace: {traceback.format_exc()}")
        return resp


async def process_function_reply(
    function_call_name,
    function_response,
    message,
    og_message,
    function_dict,
    function_metadata,
    username,
    merge=True,
    users_dir=USERS_DIR,
    chat_id=None,
):
    await MessageSender.send_debug(
        f"processing function {function_call_name} response: {str(function_response)}",
        1,
        "pink",
        username,
    )
    new_message = {
        "functionresponse": "yes",
        "color": "red",
        "message": function_response,
        "chat_id": chat_id,
    }
    await MessageSender.send_message(new_message, "red", username)

    second_response = None
    function_dict, function_metadata = await AddonManager.load_addons(
        username, users_dir
    )
    system_prompt = prompts.function_reply_system_prompt
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{message}"},
    ]
    settings = await SettingsManager.load_settings(users_dir, username)
    if settings.get("active_model").get("active_model").startswith("gpt"):
        messages.append(
            {
                "role": "function",
                "name": function_call_name,
                "content": str(function_response),
            },
        )
    else:
        messages.append(
            {
                "role": "assistant",
                "content": str(function_response),
            }
        )
    default_par = default_params
    default_par["max_tokens"] = settings.get("memory", {}).get("output", 1000)
    default_par["model"] = settings.get("active_model").get("active_model")
    responder = llmcalls.get_responder(
        (
            api_keys["openai"]
            if settings.get("active_model").get("active_model").startswith("gpt")
            else api_keys["anthropic"]
        ),
        settings.get("active_model").get("active_model"),
        default_par,
    )
    async for resp in responder.get_response(
        username,
        messages,
        stream=True,
        function_metadata=function_metadata,
        chat_id=chat_id,
    ):
        second_response = resp
    return second_response


async def queryRewrite(query, username, user_dir, memories):
    # Get the notes from the user
    notes_string = get_notes_as_string(user_dir, username)

    # Format the memories
    formatted_memories = [format_memory(memory) for memory in memories]

    relevant_memories_string = "Relevant memories:\n" + "\n".join(formatted_memories)

    current_date_time = await SettingsManager.get_current_date_time(username)
    messages = [
        {
            "role": "system",
            "content": f"Current date: {current_date_time}\nYou will get a query, notes and relevant memories. Your task is to reply with a rewritten query, if the query is about one of the relevant notes or memories, please include that in the rewritten query. Else just rewrite the query with a similar subject or synonym. For example, the user asks: what meals did you suggest to me yesterday? If the episodic memory includes details about for example a tortilla, you can rewrite the query to 'tortilla recipes'. Its important to keep this list short and concise, do not chat with ther user, only rewrite the query, nothing else!",
        },
        {
            "role": "user",
            "content": f"Notes: {notes_string}\n\nRelevant memories: {relevant_memories_string}\n\nQuery: {query}",
        },
    ]
    settings = await SettingsManager.load_settings(user_dir, username)
    default_par = default_params
    default_par["max_tokens"] = settings.get("memory", {}).get("output", 1000)
    default_par["model"] = settings.get("active_model").get("active_model")
    responder = llmcalls.get_responder(
        (
            api_keys["openai"]
            if settings.get("active_model").get("active_model").startswith("gpt")
            else api_keys["anthropic"]
        ),
        settings.get("active_model").get("active_model"),
        default_par,
    )
    async for resp in responder.get_response(
        username,
        messages,
        stream=False,
        function_metadata=fakedata,
    ):
        rewritten_query = resp

    return rewritten_query


def convert_username(username):
    # Convert non-ASCII characters to ASCII
    name = unidecode(username)
    # replace spaces and @ with underscores
    name = name.replace(" ", "_")
    name = name.replace("@", "_")
    name = name.replace(".", "_")
    # lowercase the name
    username = name.lower()
    return username


def get_notes_as_string(user_dir, username):
    notes_dir = os.path.join(user_dir, username, "notes")
    notes_string = ""

    if not os.path.exists(notes_dir):
        return notes_string

    for filename in os.listdir(notes_dir):
        file_path = os.path.join(notes_dir, filename)
        if os.path.isfile(file_path):
            with open(file_path, "r") as file:
                notes_string += f"--- {filename} ---\n"
                notes_string += file.read()
                notes_string += "\n\n"

    return notes_string.strip()


def format_memory(memory):
    date = datetime.fromtimestamp(memory["metadata"]["created_at"]).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    user_type = memory["metadata"]["username"]
    document = memory["document"]
    distance = memory["distance"]
    return f"({date}) [{user_type}]: {document} ({distance:.4f})"


import re
from typing import Tuple, List
from run_python_code import run_python_code


async def extract_and_execute_code(text: str, username: str) -> str:
    pip_packages = extract_pip_packages(text)
    code = extract_code(text)

    if not code:
        return "No code to execute."

    result = await run_python_code(code, pip_packages, username=username)

    return format_result(result)


def extract_pip_packages(text: str) -> List[str]:
    pip_match = re.search(r"<pip_install>(.*?)</pip_install>", text, re.DOTALL)
    if pip_match:
        # Extract the matched content and split it into lines
        lines = pip_match.group(1).strip().split("\n")
        # Exclude lines that start with a #
        packages = [line.strip() for line in lines if not line.strip().startswith("#")]
        # Split the remaining lines by commas
        packages = ",".join(packages).split(",")
        # Return the cleaned list of packages
        return [pkg.strip() for pkg in packages if pkg.strip()]
    return []


def extract_code(text: str) -> str:
    code_match = re.search(r"<execute_code>(.*?)</execute_code>", text, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()
    return ""


def format_result(result: dict) -> str:
    output = ""
    if "pip" in result and result["pip"]:
        output += f"Pip installation:\n{result['pip']}\n\n"
    if "output" in result:
        output += f"Code execution output:\n{result['output']}\n\n"
    if "exit_code" in result and result["exit_code"] != "0":
        output += f"Exit code: {result['exit_code']}\n"
    if "error" in result:
        output += f"Error: {result['error']}\n"
    return output


async def get_audio_duration(audio_path) -> int:
    try:
        audio = audio_segment.AudioSegment.from_file(audio_path)
        return len(audio) / 1000
    except Exception as e:
        logger.error(f"Error getting audio duration: {e}")
        return 0


def get_available_models():
    available_models = []
    if os.environ.get("OPENAI_API_KEY"):
        available_models.extend(["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"])
    if os.environ.get("ANTHROPIC_API_KEY"):
        available_models.extend(
            ["claude-3-5-sonnet-20240620", "claude-3-opus-20240229"]
        )
    return available_models
