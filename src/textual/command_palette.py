"""The Textual command palette."""

from __future__ import annotations

from abc import ABC, abstractmethod
from asyncio import CancelledError, Queue, TimeoutError, wait_for
from functools import total_ordering
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, ClassVar, NamedTuple

from rich.align import Align
from rich.console import Group, RenderableType
from rich.emoji import Emoji
from rich.style import Style
from rich.text import Text
from typing_extensions import Final, TypeAlias

from . import on, work
from ._asyncio import create_task
from ._fuzzy import Matcher
from .binding import Binding, BindingType
from .containers import Horizontal, Vertical
from .events import Click, Mount
from .reactive import var
from .screen import ModalScreen, Screen
from .timer import Timer
from .widget import Widget
from .widgets import Button, Input, LoadingIndicator, OptionList, Static
from .widgets.option_list import Option
from .worker import get_current_worker

if TYPE_CHECKING:
    from .app import App, ComposeResult

__all__ = [
    "CommandMatches",
    "CommandPalette",
    "CommandPaletteCallable",
    "CommandSource",
    "CommandSourceHit",
    "Matcher",
]


CommandPaletteCallable: TypeAlias = Callable[[], Any]
"""The type of a function that will be called when a command is selected from the command palette."""


@total_ordering
class CommandSourceHit(NamedTuple):
    """Holds the details of a single command search hit."""

    match_value: float
    """The match value of the command hit.

    The value should be between 0 (no match) and 1 (complete match).
    """

    match_display: RenderableType
    """The Rich renderable representation of the hit.

    Ideally a [rich Text object][rich.text.Text] object or similar.
    """

    command: CommandPaletteCallable
    """The function to call when the command is chosen."""

    command_text: str
    """The command text associated with the hit, as plain text.

    This is the text that will be placed into the `Input` field of the
    [command palette][textual.command_palette.CommandPalette] when a
    selection is made.
    """

    command_help: str | None = None
    """Optional help text for the command."""

    def __lt__(self, other: object) -> bool:
        if isinstance(other, CommandSourceHit):
            return self.match_value < other.match_value
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CommandSourceHit):
            return self.match_value == other.match_value
        return NotImplemented


CommandMatches: TypeAlias = AsyncIterator[CommandSourceHit]
"""Return type for the command source match searching method."""


class CommandSource(ABC):
    """Base class for command palette command sources.

    To create a source of commands inherit from this class and implement
    [`search`][textual.command_palette.CommandSource.search].
    """

    def __init__(self, screen: Screen, match_style: Style | None = None) -> None:
        """Initialise the command source.

        Args:
            screen: A reference to the active screen.
        """
        self.__screen = screen
        self.__match_style = match_style

    @property
    def focused(self) -> Widget | None:
        """The currently-focused widget in the currently-active screen in the application.

        If no widget has focus this will be `None`.
        """
        return self.__screen.focused

    @property
    def screen(self) -> Screen[object]:
        """The currently-active screen in the application."""
        return self.__screen

    @property
    def app(self) -> App[object]:
        """A reference to the application."""
        return self.__screen.app

    @property
    def match_style(self) -> Style | None:
        """The preferred style to use when highlighting matching portions of the `match_display`."""
        return self.__match_style

    def matcher(self, user_input: str, case_sensitive: bool = False) -> Matcher:
        """Create a [fuzzy matcher][textual._fuzzy.Matcher] for the given user input.

        Args:
            user_input: The text that the user has input.
            case_sensitive: Should match be case sensitive?

        Returns:
            A [fuzzy matcher][textual._fuzzy.Matcher] object for matching against candidate hits.
        """
        return Matcher(
            user_input, match_style=self.match_style, case_sensitive=case_sensitive
        )

    @abstractmethod
    async def search(self, query: str) -> CommandMatches:
        """A request to search for commands relevant to the given query.

        Args:
            query: The user input to be matched.

        Yields:
            Instances of [`CommandSourceHit`][textual.command_palette.CommandSourceHit].
        """
        raise NotImplemented


@total_ordering
class Command(Option):
    """Class that holds a command in the [`CommandList`][textual.command_palette.CommandList]."""

    def __init__(
        self,
        prompt: RenderableType,
        command: CommandSourceHit,
        id: str | None = None,
        disabled: bool = False,
    ) -> None:
        """Initialise the option.

        Args:
            prompt: The prompt for the option.
            command: The details of the command associated with the option.
            id: The optional ID for the option.
            disabled: The initial enabled/disabled state. Enabled by default.
        """
        super().__init__(prompt, id, disabled)
        self.command = command
        """The details of the command associated with the option."""

    def __lt__(self, other: object) -> bool:
        if isinstance(other, Command):
            return self.command < other.command
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Command):
            return self.command == other.command
        return NotImplemented


class CommandList(OptionList, can_focus=False):
    """The command palette command list."""

    DEFAULT_CSS = """
    CommandList {
        visibility: hidden;
        border-top: blank;
        border-bottom: hkey $accent;
        border-left: none;
        border-right: none;
        height: auto;
        max-height: 70vh;
        background: $panel;
    }

    CommandList:focus {
        border: blank;
    }

    CommandList.--visible {
        visibility: visible;
    }

    CommandList.--populating {
        border-bottom: none;
    }

    CommandList > .option-list--option-highlighted {
        background: $accent;
    }

    CommandList > .option-list--option {
        padding-left: 1;
    }
    """


class SearchIcon(Static, inherit_css=False):
    """Widget for displaying a search icon before the command input."""

    DEFAULT_CSS = """
    SearchIcon {
        margin-left: 1;
        margin-top: 1;
        width: 2;
    }
    """

    icon: var[str] = var(Emoji.replace(":magnifying_glass_tilted_right:"))
    """The icon to display."""

    def render(self) -> RenderableType:
        """Render the icon."""
        return self.icon


class CommandInput(Input):
    """The command palette input control."""

    DEFAULT_CSS = """
    CommandInput, CommandInput:focus {
        border: blank;
        width: 1fr;
        background: $panel;
        padding-left: 0;
    }
    """


class CommandPalette(ModalScreen[CommandPaletteCallable], inherit_css=False):
    """The Textual command palette."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "command-palette--help-text",
        "command-palette--highlight",
    }
    """
    | Class | Description |
    | :- | :- |
    | `command-palette--help-text` | Targets the help text of a matched command. |
    | `command-palette--highlight` | Targets the highlights of a matched command. |
    """

    DEFAULT_CSS = """
    CommandPalette {
        background: $background 30%;
        align-horizontal: center;
    }

    CommandPalette > .command-palette--help-text {
        text-style: dim;
        background: transparent;
    }

    CommandPalette > .command-palette--highlight {
        text-style: bold reverse;
        color: $success;
    }

    CommandPalette > Vertical {
        margin-top: 3;
        width: 90%;
        height: 100%;
        visibility: hidden;
    }

    CommandPalette #--input {
        height: auto;
        visibility: visible;
        border: hkey $accent;
        background: $panel;
    }

    CommandPalette #--input.--list-visible {
        border-bottom: none;
    }

    CommandPalette #--input Label {
        margin-top: 1;
        margin-left: 1;
    }

    CommandPalette #--input Button {
        min-width: 7;
    }

    CommandPalette #--results {
        overlay: screen;
        height: auto;
    }

    CommandPalette LoadingIndicator {
        height: auto;
        visibility: hidden;
        background: $panel;
        border-bottom: hkey $accent;
    }

    CommandPalette LoadingIndicator.--visible {
        visibility: visible;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+end, shift+end", "command_list('last')", show=False),
        Binding("ctrl+home, shift+home", "command_list('first')", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("escape", "escape", "Exit the command palette"),
        Binding("pagedown", "command_list('page_down')", show=False),
        Binding("pageup", "command_list('page_up')", show=False),
        Binding("up", "command_list('cursor_up')", show=False),
    ]
    """
    | Key(s) | Description |
    | :- | :- |
    | ctrl+end, shift+end | Jump to the last available commands. |
    | ctrl+home, shift+home | Jump to the first available commands. |
    | down | Navigate down through the available commands. |
    | escape | Exit the command palette. |
    | pagedown | Navigate down a page through the available commands. |
    | pageup | Navigate up a page through the available commands. |
    | up | Navigate up through the available commands. |
    """

    run_on_select: ClassVar[bool] = True
    """A flag to say if a command should be run when selected by the user.

    If `True` then when a user hits `Enter` on a command match in the result
    list, or if they click on one with the mouse, the command will be
    selected and run. If set to `False` the input will be filled with the
    command and then `Enter` should be pressed on the keyboard or the 'go'
    button should be pressed.
    """

    _list_visible: var[bool] = var(False, init=False)
    """Internal reactive to toggle the visibility of the command list."""

    _show_busy: var[bool] = var(False, init=False)
    """Internal reactive to toggle the visibility of the busy indicator."""

    _calling_screen: var[Screen | None] = var(None)
    """A record of the screen that was active when we were called."""

    _PALETTE_ID: Final[str] = "--command-palette"
    """The internal ID for the command palette."""

    def __init__(self) -> None:
        """Initialise the command palette."""
        super().__init__(id=self._PALETTE_ID)
        self._selected_command: CommandSourceHit | None = None
        """The command that was selected by the user."""
        self._busy_timer: Timer | None = None
        """Keeps track of if there's a busy indication timer in effect."""

    @staticmethod
    def is_open(app: App) -> bool:
        """Is the command palette current open?

        Args:
            app: The app to test.

        Returns:
            `True` if the command palette is currently open, `False` if not.
        """
        return app.screen.id == CommandPalette._PALETTE_ID

    @property
    def _sources(self) -> set[type[CommandSource]]:
        """The currently available command sources.

        This is a combination of the command sources defined [in the
        application][textual.app.App.COMMAND_SOURCES] and those [defined in
        the current screen][textual.screen.Screen.COMMAND_SOURCES].
        """
        return (
            set()
            if self._calling_screen is None
            else self.app.COMMAND_SOURCES | self._calling_screen.COMMAND_SOURCES
        )

    def compose(self) -> ComposeResult:
        """Compose the command palette.

        Returns:
            The content of the screen.
        """
        with Vertical():
            with Horizontal(id="--input"):
                yield SearchIcon()
                yield CommandInput(placeholder="Search...")
                if not self.run_on_select:
                    yield Button("\u25b6")
            with Vertical(id="--results"):
                yield CommandList()
                yield LoadingIndicator()

    def _on_click(self, event: Click) -> None:
        """Handle the click event.

        Args:
            event: The click event.

        This method is used to allow clicking on the 'background' as a
        method of dismissing the palette.
        """
        if self.get_widget_at(event.screen_x, event.screen_y)[0] is self:
            self.workers.cancel_all()
            self.dismiss()

    def _on_mount(self, _: Mount) -> None:
        """Capture the calling screen."""
        self._calling_screen = self.app.screen_stack[-2]

    def _stop_busy_countdown(self) -> None:
        """Stop any busy countdown that's in effect."""
        if self._busy_timer is not None:
            self._busy_timer.stop()
            self._busy_timer = None

    def _start_busy_countdown(self) -> None:
        """Start a countdown to showing that we're busy searching."""
        self._stop_busy_countdown()

        def _become_busy() -> None:
            if self._list_visible:
                self._show_busy = True

        self._busy_timer = self._busy_timer = self.set_timer(0.5, _become_busy)

    def _watch__list_visible(self) -> None:
        """React to the list visible flag being toggled."""
        self.query_one(CommandList).set_class(self._list_visible, "--visible")
        self.query_one("#--input", Horizontal).set_class(
            self._list_visible, "--list-visible"
        )
        if not self._list_visible:
            self._show_busy = False

    async def _watch__show_busy(self) -> None:
        """React to the show busy flag being toggled.

        This watcher adds or removes a busy indication depending on the
        flag's state.
        """
        self.query_one(LoadingIndicator).set_class(self._show_busy, "--visible")
        self.query_one(CommandList).set_class(self._show_busy, "--populating")

    @staticmethod
    async def _consume(
        source: CommandMatches, commands: Queue[CommandSourceHit]
    ) -> None:
        """Consume a source of matching commands, feeding the given command queue.

        Args:
            source: The source to consume.
            commands: The command queue to feed.
        """
        async for hit in source:
            await commands.put(hit)

    async def _search_for(self, search_value: str) -> CommandMatches:
        """Search for a given search value amongst all of the command sources.

        Args:
            search_value: The value to search for.

        Yields:
            The hits made amongst the registered command sources.
        """

        # Get the style for highlighted parts of a hit match.
        match_style = self._sans_background(
            self.get_component_rich_style("command-palette--highlight")
        )

        # Set up a queue to stream in the command hits from all the sources.
        commands: Queue[CommandSourceHit] = Queue()

        # Fire up an instance of each command source, inside a task, and
        # have them go start looking for matches.
        searches = [
            create_task(
                self._consume(
                    source(self._calling_screen, match_style).search(search_value),
                    commands,
                )
            )
            for source in self._sources
        ]

        # Set up a delay for showing that we're busy.
        self._start_busy_countdown()

        # Assume the search isn't aborted.
        aborted = False

        # Now, while there's some task running...
        while not aborted and any(not search.done() for search in searches):
            try:
                # ...briefly wait for something on the stack. If we get
                # something yield it up to our caller.
                aborted = yield await wait_for(commands.get(), 0.1)
                if aborted:
                    break
            except TimeoutError:
                # A timeout is fine. We're just going to go back round again
                # and see if anything else has turned up.
                pass
            except CancelledError:
                # A cancelled error means things are being aborted.
                aborted = True
            else:
                # There was no timeout, which means that we managed to yield
                # up that command; we're done with it so let the queue know.
                commands.task_done()

        # Having finished the main processing loop, we're not busy any more.
        # Anything left in the queue (see next) will fall out more or less
        # instantly.
        self._stop_busy_countdown()

        # If all the sources are pretty fast it could be that we've reached
        # this point but the queue isn't empty yet. So here we flush the
        # queue of anything left.
        while not aborted and not commands.empty():
            try:
                aborted = yield await commands.get()
            except TimeoutError:
                pass

        # If we were aborted, ensure that all of the searches are cancelled.
        if aborted:
            for search in searches:
                search.cancel()

    @staticmethod
    def _sans_background(style: Style) -> Style:
        """Returns the given style minus the background color.

        Args:
            style: The style to remove the color from.

        Returns:
            The given style, minus its background.
        """
        # Here we're pulling out all of the styles *minus* the background.
        # This should probably turn into a utility method on Style
        # eventually. The reason for this is we want the developer to be
        # able to style the help text with a component class, but we want
        # the background to always be the background at any given moment in
        # the context of an OptionList. At the moment this act of copying
        # sans bgcolor seems to be the only way to achieve this.
        return Style(
            blink2=style.blink2,
            blink=style.blink,
            bold=style.bold,
            color=style.color,
            conceal=style.conceal,
            dim=style.dim,
            encircle=style.encircle,
            frame=style.frame,
            italic=style.italic,
            link=style.link,
            overline=style.overline,
            reverse=style.reverse,
            strike=style.strike,
            underline2=style.underline2,
            underline=style.underline,
        )

    def _refresh_command_list(
        self, command_list: CommandList, commands: list[Command]
    ) -> None:
        """Refresh the command list.

        Args:
            command_list: The widget that shows the list of commands.
            commands: The commands to show in the widget.
        """
        # For the moment, this is a fairly naive approach to populating the
        # command list with a sorted list of commands. Every time we add a
        # new one we're nuking the list of options and populating them
        # again. If this turns out to not be a great approach, we may try
        # and get a lot smarter with this (ideally OptionList will grow a
        # method to sort its content in an efficient way; but for now we'll
        # go with "worse is better" wisdom).

        # First off, we sort the commands, best to worst.
        sorted_commands = sorted(commands, reverse=True)

        # If the newly-appended command is still at the end after we've
        # sorted...
        if sorted_commands[-1] == commands[-1]:
            # ...we can just add the command to the option list without
            # further fuss.
            command_list.add_option(commands[-1])
        else:
            # Nope, it's slotting in somewhere other than at the end, so
            # we'll remember where we were, clear the commands in the list,
            # add the sorted set back and apply the highlight again. Note
            # that remembering where we were is remembering the option we
            # were on, not the index.
            highlighted = (
                command_list.get_option_at_index(command_list.highlighted)
                if command_list.highlighted is not None
                else None
            )
            command_list.clear_options().add_options(sorted_commands)
            if highlighted is not None:
                command_list.highlighted = command_list.get_option_index(highlighted.id)

    @work(exclusive=True)
    async def _gather_commands(self, search_value: str) -> None:
        """Gather up all of the commands that match the search value.

        Args:
            search_value: The value to search for.
        """

        # We'll potentially use the help text style a lot so let's grab it
        # the once for use in the loop further down.
        help_style = self._sans_background(
            self.get_component_rich_style("command-palette--help-text")
        )

        # The list to hold on to the commands we've gathered from the
        # command sources.
        gathered_commands: list[Command] = []

        # Get a reference to the widget that we're going to drop the
        # (display of) the commands into.
        command_list = self.query_one(CommandList)

        # Each command will receive a sequential ID. This is going to be
        # used to find commands back again when we update the visible list
        # and want to settle the selection back on the command it was on.
        command_id = 0

        # We're going to be checking in on the worker as we loop around, so
        # grab a reference to that.
        worker = get_current_worker()

        # Go into a busy mode.
        self._show_busy = False

        # Kick off the search, grabbing the iterator.
        search = self._search_for(search_value).__aiter__()

        # We've going to be doing the send/await dance in this code, so we
        # need to grab the first yielded command to start things off.
        try:
            hit = await search.__anext__()
        except StopAsyncIteration:
            # We've been stopped before we've even really got going, likely
            # because the user is very quick on the keyboard.
            hit = None

        while hit:
            # Turn the command into something for display, and add it to the
            # list of commands that have been gathered so far.
            prompt = hit.match_display
            if hit.command_help:
                prompt = Group(prompt, Text(hit.command_help, style=help_style))
            gathered_commands.append(Command(prompt, hit, id=str(command_id)))

            # Before we go making any changes to the UI, we do a quick
            # double-check that the worker hasn't been cancelled. There's
            # little point in doing UI work on a value that isn't needed any
            # more.
            if worker.is_cancelled:
                break

            # Having made it this far, it's safe to update the list of
            # commands that match the input.
            self._refresh_command_list(command_list, gathered_commands)

            # Bump the ID.
            command_id += 1

            # Finally, get the get available command from the incoming
            # queue; note that we send the worker cancelled status down into
            # the search method.
            try:
                hit = await search.asend(worker.is_cancelled)
            except StopAsyncIteration:
                break

        # One way or another, we're not busy any more.
        self._show_busy = False

        # If we didn't get any hits, and we're not cancelled, that would
        # mean nothing was found. Give the user positive feedback to that
        # effect.
        if command_list.option_count == 0 and not worker.is_cancelled:
            command_list.add_option(
                Option(Align.center(Text("No matches found")), disabled=True)
            )

    @on(Input.Changed)
    def _input(self, event: Input.Changed) -> None:
        """React to input in the command palette.

        Args:
            event: The input event.
        """
        search_value = event.value.strip()
        self._list_visible = bool(search_value)
        self.workers.cancel_all()
        self.query_one(CommandList).clear_options()
        if search_value:
            self._gather_commands(search_value)

    @on(OptionList.OptionSelected)
    def _select_command(self, event: OptionList.OptionSelected) -> None:
        """React to a command being selected from the dropdown.

        Args:
            event: The option selection event.
        """
        event.stop()
        input = self.query_one(CommandInput)
        with self.prevent(Input.Changed):
            assert isinstance(event.option, Command)
            input.value = str(event.option.command.command_text)
            self._selected_command = event.option.command
        input.action_end()
        self._list_visible = False
        if self.run_on_select:
            self._select_or_command()

    @on(Input.Submitted)
    @on(Button.Pressed)
    def _select_or_command(self) -> None:
        """Depending on context, select or execute a command."""
        # If the list is visible, that means we're in "pick a command"
        # mode...
        if self._list_visible:
            # ...so if nothing in the list is highlighted yet...
            if self.query_one(CommandList).highlighted is None:
                # ...cause the first completion to be highlighted.
                self._action_cursor_down()
            else:
                # The list is visible, something is highlighted, the user
                # made a selection "gesture"; let's go select it!
                self._action_command_list("select")
        else:
            # The list isn't visible, which means that if we have a
            # command...
            if self._selected_command is not None:
                # ...we should return it to the parent screen and let it
                # decide what to do with it (hopefully it'll run it).
                self.workers.cancel_all()
                self.dismiss(self._selected_command.command)

    def _action_escape(self) -> None:
        """Handle a request to escape out of the command palette."""
        if self._list_visible:
            self._list_visible = False
        else:
            self.workers.cancel_all()
            self.dismiss()

    def _action_command_list(self, action: str) -> None:
        """Pass an action on to the [`CommandList`][textual.command_palette.CommandList].

        Args:
            action: The action to pass on to the [`CommandList`][textual.command_palette.CommandList].
        """
        try:
            command_action = getattr(self.query_one(CommandList), f"action_{action}")
        except AttributeError:
            return
        command_action()

    def _action_cursor_down(self) -> None:
        """Handle the cursor down action.

        This allows the cursor down key to either open the command list, if
        it's closed but has options, or if it's open with options just
        cursor through them.
        """
        if self.query_one(CommandList).option_count and not self._list_visible:
            self._list_visible = True
            self.query_one(CommandList).highlighted = 0
        else:
            self._action_command_list("cursor_down")
