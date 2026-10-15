# Teste de sicronizaçao
import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone

from banana import Banana
from bot import Bot
from omni import Omni
from lyria import Lyria
from search import WebSearch
from config import config, load_env
from discord import Discord
from gateway import Gateway
from google_auth import GoogleAuth
from store import Store
from vertex import Vertex


def log(event, **fields):
    payload = {'time': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'event': event, **fields}
    print(json.dumps(payload, separators=(',', ':')), flush=True)


async def run(cfg):
    os.umask(0o077)
    store = Store(cfg.db)
    store.cleanup(cfg.ttl)
    discord = Discord(cfg.botToken)
    auth = GoogleAuth()
    bot = Bot(cfg, store, Vertex(cfg, auth), discord, log,
              Banana(cfg, auth) if cfg.enableImages else None,
              Omni(cfg, auth) if cfg.enableVideos else None,
              WebSearch(cfg, auth) if cfg.enableSearch else None,
              lyria=Lyria(cfg, auth) if cfg.enableMusic else None)
    bot_id = cfg.appId
    event_work = set()
    stopping = False
    exit_code = 0
    gateway = None
    server = None
    cleanup_task = None
    shutdown_event = asyncio.Event()

    async def handle_event(typ, data):
        nonlocal bot_id, exit_code
        if typ == 'READY':
            bot_id = data['user']['id']
            discord.me = data.get('user')
            app = (data or {}).get('application') or {}
            if app.get('id') and app.get('id') != cfg.appId:
                log('application_id_mismatch')
                exit_code = 1
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(shutdown()))
                return
            log('discord_ready')
        if typ == 'INTERACTION_CREATE':
            await bot.interaction(data, lambda response, _d=data: discord.callback(_d, response))
        if typ == 'MESSAGE_CREATE':
            await bot.mention(data, bot_id)

    def on_gateway_event(typ, data):
        if stopping or typ not in ('READY', 'INTERACTION_CREATE', 'MESSAGE_CREATE'):
            return None
        task = asyncio.get_running_loop().create_task(handle_event(typ, data))
        event_work.add(task)
        task.add_done_callback(lambda t: event_work.discard(t))
        return task

    gateway = Gateway(discord, on_gateway_event, log, message_content=cfg.messageContent)

    async def fatal():
        nonlocal exit_code
        exit_code = 1
        await shutdown()

    async def handle_http(reader, writer):
        try:
            data = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=5)
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        line = data.split(b'\r\n', 1)[0].decode('latin1', 'replace')
        parts = line.split()
        method, path = (parts[0], parts[1]) if len(parts) >= 2 else ('', '')
        if method != 'GET' or path not in ('/healthz', '/readyz'):
            writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        else:
            try:
                database_ready = store.healthy()
            except Exception:
                database_ready = False
            ok = (not bot.closing) and (path == '/healthz' or (gateway.ready and database_ready))
            status = 200 if ok else 503
            reason = 'OK' if ok else 'Service Unavailable'
            details = {'ok': ok}
            if path == '/readyz':
                details.update(gateway=gateway.ready, database=database_ready)
            body = json.dumps(details, separators=(',', ':')).encode('utf-8')
            writer.write(
                f'HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n'
                f'Cache-Control: no-store\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode('ascii') + body
            )
        try:
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def cleanup_loop():
        while True:
            await asyncio.sleep(60)
            try:
                store.cleanup(cfg.ttl)
            except Exception:
                log('storage_cleanup_failed')

    async def shutdown():
        nonlocal stopping, exit_code
        if stopping:
            return
        stopping = True
        bot.closing = True
        gateway.stop()
        if cleanup_task:
            cleanup_task.cancel()
        if server:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            except Exception:
                pass
        try:
            async with asyncio.timeout(25):
                if event_work:
                    await asyncio.gather(*list(event_work), return_exceptions=True)
                await bot.drain()
        except TimeoutError:
            exit_code = 1
        finally:
            pending = list(event_work | bot.jobs)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if cleanup_task:
                await asyncio.gather(cleanup_task, return_exceptions=True)
        store.close()
        shutdown_event.set()

    gateway.onFatal = fatal

    try:
        server = await asyncio.start_server(handle_http, '0.0.0.0', cfg.port)
    except Exception:
        log('http_server_failed')
        exit_code = 1
        await shutdown()
        return exit_code
    log('health_server_started', port=cfg.port, model=cfg.model, region=cfg.region)
    cleanup_task = asyncio.create_task(cleanup_loop())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))
        except NotImplementedError:
            pass

    try:
        await gateway.start()
    except Exception:
        log('gateway_start_failed')
        exit_code = 1
        await shutdown()
        return exit_code

    await shutdown_event.wait()
    return exit_code


def main():
    load_env()
    try:
        cfg = config()
    except Exception as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    try:
        code = asyncio.run(run(cfg))
    except KeyboardInterrupt:
        code = 0
    sys.exit(code or 0)


if __name__ == '__main__':
    main()
