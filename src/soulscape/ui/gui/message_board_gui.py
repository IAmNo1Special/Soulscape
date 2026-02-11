import time
from typing import Callable

import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, END, LEFT, NW, RIGHT, YES, E, W, X, Y
from ttkbootstrap.scrolled import ScrolledFrame, ScrolledText

from soulscape.core import Message, MessageBoard, Operator
from soulscape.utils.helpers import safe_run_async


class FeedCard(ttk.Frame):
    """A card representing a single thread or post in the feed."""

    def __init__(
        self,
        parent: ttk.Frame,
        post: Message,
        on_click: Callable[[Message], None],
        on_delete: Callable[[str], None] | None = None,
        on_edit: Callable[[Message], None] | None = None,
        padding: int = 15,
    ):
        super().__init__(parent, bootstyle="secondary", padding=2)
        self.post = post
        self.on_click = on_click
        self.on_delete = on_delete
        self.on_edit = on_edit

        # Inner container for the card look
        self.inner = ttk.Frame(self, bootstyle="dark", padding=padding)
        self.inner.pack(fill=BOTH, expand=YES)

        # Author and Metadata
        header_frame = ttk.Frame(self.inner, bootstyle="dark")
        header_frame.pack(fill=X, pady=(0, 10))

        author_label = ttk.Label(
            header_frame,
            text=f"u/{post.author_name}",
            font=("Segoe UI", 10, "bold"),
            bootstyle="primary",
        )
        author_label.pack(side=LEFT)

        ts_str = time.strftime("%H:%M  %d %b", time.localtime(post.timestamp))
        ttk.Label(
            header_frame,
            text=f" • {ts_str}",
            font=("Segoe UI", 8),
            bootstyle="secondary",
        ).pack(side=LEFT, padx=(5, 0))

        # Title
        ttk.Label(
            self.inner,
            text=post.title,
            font=("Segoe UI", 12, "bold"),
            bootstyle="light",
            wraplength=350,
        ).pack(fill=X, anchor=NW, pady=(0, 5))

        # Content
        # We truncate content for the feed card if it's too long
        content_text = post.content
        if len(content_text) > 100:
            content_text = content_text[:97] + "..."

        content_label = ttk.Label(
            self.inner,
            text=content_text,
            font=("Segoe UI", 10),
            wraplength=350,  # Specific to 2-column layout width
            bootstyle="secondary",
        )
        content_label.pack(fill=X, anchor=NW)

        # Footer / Interactions
        footer_frame = ttk.Frame(self.inner, bootstyle="dark")
        footer_frame.pack(fill=X, pady=(15, 0))

        reply_count = self._count_replies(post)
        reply_btn = ttk.Button(
            footer_frame,
            text=f"💬 {reply_count} Replies",
            bootstyle="info-link",
            command=lambda: on_click(post),
        )
        reply_btn.pack(side=LEFT)

        # Edit/Delete Buttons (Right-aligned)
        btn_container = ttk.Frame(footer_frame, bootstyle="dark")
        btn_container.pack(side=RIGHT)

        # Show Edit if author matches or if operator
        if on_edit and (post.author_id == Operator.ID):
            ttk.Button(
                btn_container,
                text="✏️ Edit",
                bootstyle="warning-link",
                command=lambda: on_edit(post),
            ).pack(side=LEFT, padx=(0, 5))

        if on_delete:
            ttk.Button(
                btn_container,
                text="🗑️ Delete",
                bootstyle="danger-link",
                command=lambda: on_delete(post.message_id),
            ).pack(side=LEFT)

        # Bind click to the whole card (or at least the content)
        self.inner.bind("<Button-1>", lambda e: on_click(post))
        content_label.bind("<Button-1>", lambda e: on_click(post))

    def _count_replies(self, message: Message) -> int:
        count = len(message.replies)
        for reply in message.replies:
            count += self._count_replies(reply)
        return count


class MessageBoardWindow(ttk.Toplevel):
    """Window for displaying the message board."""

    def __init__(
        self,
        parent: ttk.Window,
        on_post: Callable[[int, str, str, str], None] | None = None,
        on_reply: Callable[[int, str, str, str], None] | None = None,
        on_delete: Callable[[int, str], None] | None = None,
        on_edit: Callable[[int, str, str], None] | None = None,
    ):
        """Initializes the message board window."""
        super().__init__(title="Soulscape Global Frequency", master=parent)
        self.geometry("900x700")
        self.board = MessageBoard()
        self.on_post = on_post
        self.on_reply = on_reply
        self.on_delete = on_delete
        self.on_edit = on_edit
        self.current_thread_id: str | None = None
        self._last_data_hash: str | None = None

        self._setup_ui()
        self._refresh_data()

        # Auto-refresh every 2 seconds (feed is more expensive to re-draw than tree)
        self.after(2000, self._auto_refresh)

    def _setup_ui(self) -> None:
        """Sets up the user interface."""
        # Main Container
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=BOTH, expand=YES)

        # --- Header Section ---
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=X, pady=(0, 20))

        ttk.Label(
            header_frame,
            text="Soulscape Feed",
            font=("Segoe UI", 28, "bold"),
            bootstyle="light",
        ).pack(side=LEFT)

        # Action Buttons
        btn_frame = ttk.Frame(header_frame)
        btn_frame.pack(side=RIGHT)

        ttk.Button(
            btn_frame,
            text="New Transmission",
            bootstyle="success",
            command=self._show_post_dialog,
        ).pack(side=RIGHT, padx=5)

        ttk.Button(
            btn_frame,
            text="Refresh",
            bootstyle="secondary-outline",
            command=self._refresh_data,
        ).pack(side=RIGHT, padx=5)

        # --- Content Area (Reddit Style Feed) ---
        self.scroll_frame = ScrolledFrame(
            main_container,
            autohide=True,
        )
        self.scroll_frame.pack(fill=BOTH, expand=YES)

        # We'll use a 2-column grid inside the scroll_frame's container
        self.feed_container = self.scroll_frame

    def _refresh_data(self) -> None:
        """Refreshes the message data from the board."""
        safe_run_async(self.board.refresh())
        posts = self.board.get_recent_posts(50)

        current_hash = "|".join(
            [
                f"{p.message_id}:{p.timestamp}:{hash(p.content)}:{self._get_total_replies(p)}"
                for p in posts
            ]
        )

        if current_hash == self._last_data_hash:
            return

        self._last_data_hash = current_hash

        # Clear existing cards
        for widget in self.feed_container.winfo_children():
            widget.destroy()

        # Grid layout for 2 columns
        for i, post in enumerate(posts):
            row = i // 2
            col = i % 2

            card = FeedCard(
                self.feed_container,
                post,
                on_click=self._on_card_click,
                on_delete=self._on_card_delete,
                on_edit=self._on_card_edit,
            )
            card.grid(row=row, column=col, sticky="nsew", padx=10, pady=10)

        # Configure grid weights for 2 columns
        self.feed_container.columnconfigure(0, weight=1)
        self.feed_container.columnconfigure(1, weight=1)

    def _get_total_replies(self, message: Message) -> int:
        """Helper to get total reply count for hashing."""
        count = len(message.replies)
        for reply in message.replies:
            count += self._get_total_replies(reply)
        return count

    def _on_card_click(self, post: Message) -> None:
        """Handles click on a card to open thread view."""
        ThreadViewDialog(
            self,
            self.board,
            post,
            on_reply=self.on_reply,
            on_delete=self.on_delete,
            on_edit=self.on_edit,
        )

    def _on_card_delete(self, message_id: str) -> None:
        """Handles deletion of a message."""
        if self.on_delete:
            self.on_delete(Operator.ID, message_id)
            # Immediate feedback/refresh
            self.after(500, self._refresh_data)

    def _on_card_edit(self, post: Message) -> None:
        """Handles editing of a message."""
        EditDialog(
            self, post, on_success=self._refresh_data, on_edit=self.on_edit
        )

    def _auto_refresh(self) -> None:
        """Automatically refreshes data if the window is open."""
        if self.winfo_exists():
            # Only refresh if data file actually changed to avoid flickering
            safe_run_async(self.board.refresh())
            self._refresh_data()
            self.after(5000, self._auto_refresh)

    def _show_post_dialog(self) -> None:
        """Shows the dialog to create a new post."""
        PostDialog(
            self,
            self.board,
            on_success=self._refresh_data,
            on_post=self.on_post,
        )


class PostDialog(ttk.Toplevel):
    """Dialog for creating a new post."""

    def __init__(
        self,
        parent: ttk.Window,
        board: MessageBoard,
        on_success: Callable[[], None],
        on_post: Callable[[int, str, str, str], None] | None = None,
    ):
        super().__init__(title="New Transmission", master=parent)
        self.geometry("500x350")
        self.board = board
        self.on_success = on_success
        self.on_post = on_post

        self._setup_ui()

    def _setup_ui(self) -> None:
        container = ttk.Frame(self, padding=20)
        container.pack(fill=BOTH, expand=YES)

        ttk.Label(
            container,
            text="Title",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=W, pady=(0, 5))

        self.title_entry = ttk.Entry(container, font=("Segoe UI", 11))
        self.title_entry.pack(fill=X, pady=(0, 15))

        ttk.Label(
            container,
            text="Message",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=W, pady=(0, 5))

        self.text_area = ScrolledText(
            container, height=8, autohide=True, font=("Segoe UI", 10)
        )
        self.text_area.pack(fill=BOTH, expand=YES, pady=(0, 20))

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=X)

        ttk.Button(
            btn_frame,
            text="Cancel",
            bootstyle="secondary",
            command=self.destroy,
        ).pack(side=LEFT)

        ttk.Button(
            btn_frame, text="Broadcast", bootstyle="success", command=self._send
        ).pack(side=RIGHT)

    def _send(self) -> None:
        title = self.title_entry.get().strip()
        content = self.text_area.text.get("1.0", END).strip()
        if title and content:
            if self.on_post:
                self.on_post(Operator.ID, Operator.NAME, title, content)
            else:
                safe_run_async(
                    self.board.create_post(
                        Operator.ID, Operator.NAME, title, content
                    )
                )

            self.on_success()
            self.destroy()


class ThreadViewDialog(ttk.Toplevel):
    """Dialog for viewing a message thread (Reddit-style)."""

    def __init__(
        self,
        parent: ttk.Window,
        board: MessageBoard,
        post: Message,
        on_reply: Callable[[int, str, str, str], None] | None = None,
        on_delete: Callable[[int, str], None] | None = None,
        on_edit: Callable[[int, str, str], None] | None = None,
    ):
        super().__init__(title=f"Thread by u/{post.author_name}", master=parent)
        self.geometry("700x800")
        self.board = board
        self.post = post
        self.on_reply = on_reply
        self.on_delete = on_delete
        self.on_edit = on_edit
        self._last_thread_hash: str | None = None

        self._setup_ui()

        # Start auto-refresh for thread view too
        self.after(2000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        """Automatically refreshes the thread data."""
        if not self.winfo_exists():
            return

        safe_run_async(self.board.refresh())
        updated_post = self.board.posts.get(self.post.message_id)

        if updated_post:
            self.post = updated_post
            self._render_thread()

        self.after(2000, self._auto_refresh)

    def _setup_ui(self) -> None:
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=BOTH, expand=YES)

        # Scrollable area for the thread
        self.scroll_frame = ScrolledFrame(main_container, autohide=True)
        self.scroll_frame.pack(fill=BOTH, expand=YES, pady=(0, 20))

        self._render_thread()

        # Reply Section (Sticky at bottom)
        reply_frame = ttk.Frame(
            main_container, padding=10, bootstyle="secondary"
        )
        reply_frame.pack(fill=X)

        self.reply_entry = ttk.Entry(reply_frame, font=("Segoe UI", 11))
        self.reply_entry.pack(side=LEFT, fill=X, expand=YES, padx=(0, 10))
        self.reply_entry.bind("<Return>", lambda e: self._reply())

        ttk.Button(
            reply_frame,
            text="Reply",
            bootstyle="primary",
            command=self._reply,
        ).pack(side=RIGHT)

    def _render_thread(self) -> None:
        """Renders the entire thread if changed."""
        # Detect if thread content actually changed
        current_hash = f"{self.post.message_id}:{self.post.timestamp}:{self._get_thread_hash(self.post)}"
        if current_hash == self._last_thread_hash:
            return

        self._last_thread_hash = current_hash

        # Clear existing
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        # Root Post Card (Larger)
        root_card = ttk.Frame(self.scroll_frame, bootstyle="dark", padding=20)
        root_card.pack(fill=X, pady=(0, 20))

        ttk.Label(
            root_card,
            text=f"u/{self.post.author_name}",
            font=("Segoe UI", 12, "bold"),
            bootstyle="primary",
        ).pack(anchor=W)

        ts_str = time.strftime(
            "%H:%M  %d %b %Y", time.localtime(self.post.timestamp)
        )
        ttk.Label(
            root_card, text=ts_str, font=("Segoe UI", 8), bootstyle="secondary"
        ).pack(anchor=W)

        ttk.Label(
            root_card,
            text=self.post.title,
            font=("Segoe UI", 16, "bold"),
            bootstyle="info",
            wraplength=600,
        ).pack(anchor=W, pady=(0, 10))

        ttk.Label(
            root_card,
            text=self.post.content,
            font=("Segoe UI", 11),
            wraplength=600,
            bootstyle="light",
        ).pack(fill=X, pady=(15, 0))

        if self.on_edit and (self.post.author_id == Operator.ID):
            ttk.Button(
                root_card,
                text="Edit Transmission",
                bootstyle="warning-outline",
                command=lambda: self._edit_root(),
            ).pack(anchor=E, pady=(10, 0))

        # Replies Header
        ttk.Separator(self.scroll_frame).pack(fill=X, pady=10)
        ttk.Label(
            self.scroll_frame, text="COMMENTS", font=("Segoe UI", 10, "bold")
        ).pack(anchor=W, padx=10)

        # Nested Replies
        self._render_replies(self.post, self.scroll_frame, level=0)

    def _render_replies(
        self, parent_msg: Message, container: ttk.Frame, level: int
    ) -> None:
        for reply in parent_msg.replies:
            reply_frame = ttk.Frame(
                container, padding=(20 if level < 5 else 10, 5)
            )
            reply_frame.pack(fill=X, anchor=W)

            # Indentation visualization
            if level > 0:
                line_frame = ttk.Frame(
                    reply_frame, width=2, bootstyle="secondary"
                )
                line_frame.pack(side=LEFT, fill=Y, padx=(0, 10))

            content_frame = ttk.Frame(reply_frame)
            content_frame.pack(side=LEFT, fill=BOTH, expand=YES)

            # Metadata
            meta_frame = ttk.Frame(content_frame)
            meta_frame.pack(fill=X)

            meta_str = f"u/{reply.author_name} • {time.strftime('%H:%M', time.localtime(reply.timestamp))}"
            ttk.Label(
                meta_frame, text=meta_str, font=("Segoe UI", 9, "bold")
            ).pack(side=LEFT)

            if self.on_delete:
                ttk.Button(
                    meta_frame,
                    text="[delete]",
                    bootstyle="danger-link",
                    padding=0,
                    command=lambda r_id=reply.message_id: self._reply_delete(
                        r_id
                    ),
                ).pack(side=LEFT, padx=5)

            if self.on_edit and (reply.author_id == Operator.ID):
                ttk.Button(
                    meta_frame,
                    text="[edit]",
                    bootstyle="warning-link",
                    padding=0,
                    command=lambda r=reply: self._reply_edit(r),
                ).pack(side=LEFT, padx=5)

            # Content
            ttk.Label(
                content_frame,
                text=reply.content,
                font=("Segoe UI", 10),
                wraplength=500 - (level * 20),
            ).pack(anchor=W, pady=(2, 5))

            # Recurse for nested
            self._render_replies(reply, container, level + 1)

    def _reply_delete(self, message_id: str) -> None:
        """Handles deletion of a reply."""
        if self.on_delete:
            self.on_delete(Operator.ID, message_id)
            # Immediate feedback/refresh
            self.after(500, self._auto_refresh)

    def _reply_edit(self, reply: Message) -> None:
        """Handles editing of a reply."""
        EditDialog(
            self, reply, on_success=self._auto_refresh, on_edit=self.on_edit
        )

    def _edit_root(self) -> None:
        """Edits the root post of this thread."""
        EditDialog(
            self, self.post, on_success=self._auto_refresh, on_edit=self.on_edit
        )

    def _get_thread_hash(self, message: Message) -> str:
        """Helper to generate a hash for the entire thread structure."""
        # Include content hash to detect edits
        h = f"{message.message_id}:{message.timestamp}:{hash(message.content)}"
        for reply in message.replies:
            h += "|" + self._get_thread_hash(reply)
        return h

    def _reply(self) -> None:
        content = self.reply_entry.get().strip()
        if content:
            if self.on_reply:
                self.on_reply(
                    Operator.ID, Operator.NAME, self.post.message_id, content
                )
            else:
                safe_run_async(
                    self.board.create_reply(
                        Operator.ID,
                        Operator.NAME,
                        self.post.message_id,
                        content,
                    )
                )

            self.reply_entry.delete(0, END)
            # Re-render to show new reply
            safe_run_async(self.board.refresh())
            updated_post = self.board.posts.get(self.post.message_id)
            if updated_post:
                self.post = updated_post
                self._render_thread()


class EditDialog(ttk.Toplevel):
    """Dialog for editing an existing post or reply."""

    def __init__(
        self,
        parent: ttk.Window,
        message: Message,
        on_success: Callable[[], None],
        on_edit: Callable[[int, str, str], None] | None = None,
    ):
        super().__init__(title="Edit Transmission", master=parent)
        self.geometry("500x350")
        self.message = message
        self.on_success = on_success
        self.on_edit = on_edit

        self._setup_ui()

    def _setup_ui(self) -> None:
        container = ttk.Frame(self, padding=20)
        container.pack(fill=BOTH, expand=YES)

        ttk.Label(
            container,
            text="Edit Message",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor=W, pady=(0, 10))

        self.text_area = ScrolledText(
            container, height=8, autohide=True, font=("Segoe UI", 10)
        )
        self.text_area.pack(fill=BOTH, expand=YES, pady=(0, 20))

        # Pre-fill with existing content
        self.text_area.text.insert("1.0", self.message.content)

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill=X)

        ttk.Button(
            btn_frame,
            text="Cancel",
            bootstyle="secondary",
            command=self.destroy,
        ).pack(side=LEFT)

        ttk.Button(
            btn_frame, text="Update", bootstyle="success", command=self._save
        ).pack(side=RIGHT)

    def _save(self) -> None:
        content = self.text_area.text.get("1.0", "end").strip()
        if content:
            if self.on_edit:
                self.on_edit(Operator.ID, self.message.message_id, content)

            # Optimistic local update for immediate visual feedback
            self.message.content = content

            # Give it time for IPC to process and file to save before full reload
            self.after(500, self.on_success)
            self.destroy()
