import asyncio
import os
import time
import uuid

from fastapi import APIRouter
from fastapi import UploadFile, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.websockets import (WebSocketDisconnect, WebSocket)
from pydub import AudioSegment
from starlette.websockets import WebSocketState

from utils.stt.deepgram_util import transcribe_file_deepgram, process_audio_dg, send_initial_file, \
    get_speaker_audio_file
from utils.stt.vad import vad_is_empty, VADIterator, model

router = APIRouter()


@router.post("/transcribe")
def transcribe(file: UploadFile, uid: str, language: str = 'en'):
    upload_id = str(uuid.uuid4())
    file_path = f"_temp/{upload_id}_{file.filename}"
    with open(file_path, 'wb') as f:
        f.write(file.file.read())

    aseg = AudioSegment.from_wav(file_path)
    print(f'Transcribing audio {aseg.duration_seconds} secs and {aseg.frame_rate / 1000} khz')

    if vad_is_empty(file_path):
        os.remove(file_path)
        return []
    transcript = transcribe_file_deepgram(file_path, language=language)

    os.remove(file_path)
    return transcript  # result


@router.post("/v1/transcribe", tags=['v1'])
def transcribe_auth(file: UploadFile, uid: str, language: str = 'en'):
    upload_id = str(uuid.uuid4())
    file_path = f"_temp/{upload_id}_{file.filename}"
    with open(file_path, 'wb') as f:
        f.write(file.file.read())

    aseg = AudioSegment.from_wav(file_path)
    print(f'Transcribing audio {aseg.duration_seconds} secs and {aseg.frame_rate / 1000} khz')

    if vad_is_empty(file_path):
        os.remove(file_path)
        return []
    transcript = transcribe_file_deepgram(file_path, language=language)
    os.remove(file_path)
    return transcript  # result


templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse)
def get(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


async def _websocket_util(
        websocket: WebSocket, uid: str, language: str = 'en', sample_rate: int = 8000, codec: str = 'pcm8',
        channels: int = 1
):
    print('websocket_endpoint', uid, language, sample_rate, codec, channels)
    await websocket.accept()
    transcript_socket2 = None
    duration = 0
    try:
        # single_file_path, duration = get_speaker_audio_file(uid) if language == 'en' else (None, 0)
        # remove_downloaded_samples(uid) # do not remove them for now.
        single_file_path, duration = None, 0
        # TODO: what if opus? and the samples were created with pcm?
        transcript_socket = await process_audio_dg(websocket, language, sample_rate, codec, channels,
                                                   preseconds=duration)
        if duration:
            transcript_socket2 = await process_audio_dg(websocket, language, sample_rate, codec, channels)
            await send_initial_file(single_file_path, transcript_socket)

    except Exception as e:
        print(f"Initial processing error: {e}")
        await websocket.close()
        return

    vad_iterator = VADIterator(model, sampling_rate=sample_rate)  # threshold=0.9
    window_size_samples = 256 if sample_rate == 8000 else 512

    async def receive_audio(socket1, socket2):
        # audio_buffer = bytearray()
        timer_start = time.time()
        try:
            while True:
                data = await websocket.receive_bytes()
                # print(len(data))
                # audio_buffer.extend(data)
                # print(len(audio_buffer), window_size_samples * 2) # * 2 because 16bit
                # TODO: vad not working propperly.
                # - PCM still has to collect samples, and while it collects them, still sends them to the socket, so it's like nothing
                # - Opus always says there's no speech (but collection doesn't matter much, as it triggers like 1 per 0.2 seconds)

                # len(data) = 160, 8khz 16bit -> 2 bytes per sample, 80 samples, needs 256 samples, which is 256*2 bytes
                # if len(audio_buffer) >= window_size_samples * 2:
                #     # TODO: vad doesn't work index.html
                #     if is_speech_present(audio_buffer[:window_size_samples * 2], vad_iterator, window_size_samples):
                #         print('*')
                #         # pass
                #     else:
                #         print('-')
                #         audio_buffer = audio_buffer[window_size_samples * 2:]
                #         continue
                #
                #     audio_buffer = audio_buffer[window_size_samples * 2:]

                elapsed_seconds = time.time() - timer_start
                if elapsed_seconds > duration or not socket2:
                    socket1.send(data)
                    # print('Sending to socket 1')
                    if socket2:
                        print('Killing transcript_socket2')
                        socket2.finish()
                        socket2 = None
                else:
                    # print('Sending to socket 2')
                    socket2.send(data)

        except WebSocketDisconnect:
            print("WebSocket disconnected")
        except Exception as e:
            print(f'Could not process audio: error {e}')
        finally:
            socket1.finish()
            if socket2:
                socket2.finish()

    async def send_heartbeat():
        try:
            while True:
                await asyncio.sleep(30)
                print('send_heartbeat')
                await websocket.send_json({"type": "ping"})
        except WebSocketDisconnect:
            print("WebSocket disconnected")
        except Exception as e:
            print(f'Heartbeat error: {e}')

    try:
        receive_task = asyncio.create_task(receive_audio(transcript_socket, transcript_socket2))
        heartbeat_task = asyncio.create_task(send_heartbeat())
        await asyncio.gather(receive_task, heartbeat_task)
    except Exception as e:
        print(f"Error during WebSocket operation: {e}")
    finally:
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                await websocket.close()
            except Exception as e:
                print(f"Error closing WebSocket: {e}")


@router.websocket("/listen")
async def websocket_endpoint(
        websocket: WebSocket, uid: str, language: str = 'en', sample_rate: int = 8000, codec: str = 'pcm8',
        channels: int = 1
):
    await _websocket_util(websocket, uid, language, sample_rate, codec, channels)
