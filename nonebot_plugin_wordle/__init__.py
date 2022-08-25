import re
import shlex
import asyncio
from io import BytesIO
from dataclasses import dataclass
from asyncio import TimerHandle
from typing import Dict, List, Optional, NoReturn
from models.bag_user import BagUser

from nonebot.typing import T_State
from nonebot.matcher import Matcher
from nonebot.exception import ParserExit
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule, to_me, ArgumentParser
from nonebot import on_command, on_shell_command, on_message
from nonebot.params import ShellCommandArgv, CommandArg, EventPlainText, State
from nonebot.adapters.onebot.v11 import (
    MessageEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
)

from .utils import dic_list, random_word
from .data_source import Wordle, GuessResult

# __plugin_meta__ = PluginMetadata(
#     name="猜单词",
#     description="wordle猜单词游戏",
#     usage=(
#         "@我 + “猜单词”开始游戏；\n"
#         "答案为指定长度单词，发送对应长度单词即可；\n"
#         "绿色块代表此单词中有此字母且位置正确；\n"
#         "黄色块代表此单词中有此字母，但该字母所处位置不对；\n"
#         "灰色块代表此单词中没有此字母；\n"
#         "猜出单词或用光次数则游戏结束；\n"
#         "发送“结束”结束游戏；发送“提示”查看提示；\n"
#         "可使用 -l/--length 指定单词长度，默认为5；\n"
#         "可使用 -d/--dic 指定词典，默认为CET4\n"
#         f"支持的词典：{'、'.join(dic_list)}"
#     ),
#     extra={
#         "unique_name": "wordle",
#         "example": "@小Q 猜单词\nwordle -l 6 -d CET6",
#         "author": "meetwq <meetwq@gmail.com>",
#         "version": "0.1.10",
#     },
# )

__zx_plugin_name__ = "猜单词"
__plugin_usage__ = """
usage：
    Wordle 猜单词
    答案为指定长度单词，发送对应长度单词即可
    绿色块代表此单词中有此字母且位置正确
    黄色块代表此单词中有此字母，但该字母所处位置不对
    灰色块代表此单词中没有此字母
    猜出单词或用光次数则游戏结束
    指令：
        猜单词（需要at）：开始游戏（需要at）
        可使用 -l/--length 指定单词长度，默认为5
        可使用 -d/--dic 指定词典，默认为CET4
        
        发送“结束”结束游戏；发送“提示”查看提示；
""".strip()
__plugin_des__ = "Wordle 猜单词"
__plugin_type__ = ("群内小游戏",)
__plugin_cmd__ = ["猜单词", "结束", "提示"]
__plugin_version__ = 0.1
__plugin_author__ = "migang & meetwq"

__plugin_settings__ = {
    "level": 5,
    "default_status": True,
    "limit_superuser": False,
    "cmd": __plugin_cmd__,
}

parser = ArgumentParser("wordle", description="猜单词")
parser.add_argument("-l", "--length", type=int, default=5, help="单词长度")
parser.add_argument("-d", "--dic", default="CET4", help="词典")
parser.add_argument("--hint", action="store_true", help="提示")
parser.add_argument("--stop", action="store_true", help="结束游戏")
parser.add_argument("word", nargs="?", help="单词")


@dataclass
class Options:
    length: int = 0
    dic: str = ""
    hint: bool = False
    stop: bool = False
    word: str = ""


games: Dict[str, Wordle] = {}
timers: Dict[str, TimerHandle] = {}

wordle = on_shell_command("wordle", parser=parser, block=True, priority=13)


@wordle.handle()
async def _(
    matcher: Matcher, event: MessageEvent, argv: List[str] = ShellCommandArgv()
):
    await handle_wordle(matcher, event, argv)


def get_cid(event: MessageEvent):
    return (
        f"group_{event.group_id}"
        if isinstance(event, GroupMessageEvent)
        else f"private_{event.user_id}"
    )


def game_running(event: MessageEvent) -> bool:
    cid = get_cid(event)
    return bool(games.get(cid, None))


def get_word_input(state: T_State = State(), msg: str = EventPlainText()) -> bool:
    if re.fullmatch(r"^[a-zA-Z]{3,8}$", msg):
        state["word"] = msg
        return True
    return False


def shortcut(cmd: str, argv: List[str] = [], **kwargs):
    command = on_command(cmd, **kwargs, block=True, priority=12)

    @command.handle()
    async def _(matcher: Matcher, event: MessageEvent, msg: Message = CommandArg()):
        try:
            args = shlex.split(msg.extract_plain_text().strip())
        except:
            args = []
        await handle_wordle(matcher, event, argv + args)


shortcut("猜单词", ["--length", "5", "--dic", "CET4"], rule=to_me())
shortcut("提示", ["--hint"], aliases={"给个提示"}, rule=game_running)
shortcut("结束", ["--stop"], aliases={"停", "停止游戏", "结束游戏"}, rule=game_running)


word_matcher = on_message(Rule(game_running) & get_word_input, block=True, priority=12)


@word_matcher.handle()
async def _(matcher: Matcher, event: MessageEvent, state: T_State = State()):
    word: str = state["word"]
    await handle_wordle(matcher, event, [word])


async def stop_game(matcher: Matcher, cid: str):
    timers.pop(cid, None)
    if games.get(cid, None):
        game = games.pop(cid)
        msg = "猜单词超时，游戏结束"
        if len(game.guessed_words) >= 1:
            msg += f"\n{game.result}"
        await matcher.finish(msg)


def set_timeout(matcher: Matcher, cid: str, timeout: float = 300):
    timer = timers.get(cid, None)
    if timer:
        timer.cancel()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(
        timeout, lambda: asyncio.ensure_future(stop_game(matcher, cid))
    )
    timers[cid] = timer


async def handle_wordle(matcher: Matcher, event: MessageEvent, argv: List[str]):
    async def send(
        message: Optional[str] = None, image: Optional[BytesIO] = None
    ) -> NoReturn:
        if not (message or image):
            await matcher.finish()
        msg = Message()
        if image:
            msg.append(MessageSegment.image(image))
        if message:
            msg.append(message)
        await matcher.finish(msg)

    try:
        args = parser.parse_args(argv)
    except ParserExit as e:
        if e.status == 0:
            await send(__plugin_usage__)
        await send()

    options = Options(**vars(args))

    cid = get_cid(event)
    if not games.get(cid, None):
        if options.word:
            await send()

        if options.word or options.stop or options.hint:
            await send("没有正在进行的游戏")

        if not (options.length and options.dic):
            await send("请指定单词长度和词典")

        if options.length < 3 or options.length > 8:
            await send("单词长度应在3~8之间")

        if options.dic not in dic_list:
            await send("支持的词典：" + ", ".join(dic_list))

        word, meaning = random_word(options.dic, options.length)
        game = Wordle(word, meaning)
        games[cid] = game
        set_timeout(matcher, cid)

        await send(f"你有{game.rows}次机会猜出单词，单词长度为{game.length}，请发送单词", game.draw())

    if options.stop:
        game = games.pop(cid)
        msg = "游戏已结束"
        if len(game.guessed_words) >= 1:
            msg += f"\n{game.result}"
        await send(msg)

    game = games[cid]
    set_timeout(matcher, cid)

    if options.hint:
        hint = game.get_hint()
        if not hint.replace("*", ""):
            await send("你还没有猜对过一个字母哦~再猜猜吧~")
        await send(image=game.draw_hint(hint))

    word = options.word
    if not re.fullmatch(r"^[a-zA-Z]{3,8}$", word):
        await send()
    if len(word) != game.length:
        await send("请发送正确长度的单词")

    result = game.guess(word)
    if result in [GuessResult.WIN, GuessResult.LOSS]:
        games.pop(cid)
        if result == GuessResult.WIN:
            await BagUser.add_gold(event.user_id, event.group_id, 200)
            text = f"\n[CQ:at,qq={event.user_id}]你获得了200金币，目前金币余额为{str(await BagUser.get_gold(event.user_id, event.group_id))}"
        await send(
            (f"恭喜你猜出了单词！{text}" if result == GuessResult.WIN else "很遗憾，没有人猜出来呢")
            + f"\n{game.result}",
            game.draw(),
        )
    elif result == GuessResult.DUPLICATE:
        await send("你已经猜过这个单词了呢")
    elif result == GuessResult.ILLEGAL:
        await send(f"你确定 {word} 是一个合法的单词吗？")
    else:
        await send(image=game.draw())
