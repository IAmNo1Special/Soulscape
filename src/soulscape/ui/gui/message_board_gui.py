import time

import ttkbootstrap as ttk
from ttkbootstrap.constants import *
from ttkbootstrap.scrolled import ScrolledText

from soulscape.core.social import MessageBoard


class MessageBoardWindow(ttk.Toplevel):
    def __init__(self, parent):
        super().__init__(title="Soulscape Message Board", master=parent)
        self.geometry("800x600")

        # Apply theme only if we were the root, but we are Toplevel.
        # However, ttkbootstrap styles are global to the app usually.
        # We assume the main app might not be ttkbootstrap aware,
        # so we might need to style manually or hope it applies.
        # Note: If parent isn't ttkbootstrap, this might look odd,
        # but style is usually global in Tkinter.

        self.board = MessageBoard()
        self.current_thread_id = None

        self._setup_ui()
        self._refresh_data()

        # Auto-refresh every 5 seconds
        self.after(5000, self._auto_refresh)

    def _setup_ui(self):
        # Main Container with padding
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=BOTH, expand=YES)

        # --- Header Section ---
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=X, pady=(0, 20))

        ttk.Label(
            header_frame,
            text="Global Frequency",
            font=("Segoe UI", 24, "bold"),
            bootstyle="inverse-primary",
        ).pack(side=LEFT, padx=(0, 10))

        ttk.Label(
            header_frame,
            text="Connect with other souls across the void.",
            font=("Segoe UI", 10, "italic"),
            bootstyle="secondary",
        ).pack(side=LEFT, fill=Y, pady=10)

        # Action Buttons
        btn_frame = ttk.Frame(header_frame)
        btn_frame.pack(side=RIGHT)

        ttk.Button(
            btn_frame,
            text="New Transmission",
            bootstyle="success-outline",
            command=self._show_post_dialog,
        ).pack(side=RIGHT, padx=5)

        ttk.Button(
            btn_frame,
            text="Refresh Signal",
            bootstyle="info-outline",
            command=self._refresh_data,
        ).pack(side=RIGHT, padx=5)

        # --- Content Area ---
        # Using a modern Treeview style
        columns = ("author", "content", "timestamp")

        self.tree = ttk.Treeview(
            main_container,
            columns=columns,
            show="headings",
            bootstyle="primary",
            selectmode="browse",
        )

        # Configure Columns
        self.tree.heading("author", text="Identity")
        self.tree.heading("content", text="Transmission Content")
        self.tree.heading("timestamp", text="Time (UTC)")

        self.tree.column("author", width=120, anchor=W)
        self.tree.column("content", width=500, anchor=W)
        self.tree.column("timestamp", width=150, anchor=E)

        # Scrollbar
        scrollbar = ttk.Scrollbar(
            main_container,
            orient=VERTICAL,
            command=self.tree.yview,
            bootstyle="primary-round",
        )
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side=LEFT, fill=BOTH, expand=YES)
        scrollbar.pack(side=RIGHT, fill=Y)

        # Bindings
        self.tree.bind("<Double-1>", self._on_item_double_click)

    def _refresh_data(self):
        self.board._load_data()

        # Clear existing
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Populate
        posts = self.board.get_recent_posts(50)
        for post in posts:
            ts_str = time.strftime(
                "%H:%M  %d-%b", time.localtime(post.timestamp)
            )
            self.tree.insert(
                "",
                END,
                iid=post.message_id,
                values=(post.author_name, post.content, ts_str),
            )

    def _auto_refresh(self):
        if self.winfo_exists():
            self._refresh_data()
            self.after(5000, self._auto_refresh)

    def _show_post_dialog(self):
        PostDialog(self, self.board, on_success=self._refresh_data)

    def _on_item_double_click(self, event):
        selection = self.tree.selection()
        if not selection:
            return

        item_id = selection[0]
        post = self.board.posts.get(item_id)
        if post:
            ThreadViewDialog(self, self.board, post)


class PostDialog(ttk.Toplevel):
    def __init__(self, parent, board, on_success):
        super().__init__(title="New Transmission", master=parent)
        self.geometry("500x300")
        self.board = board
        self.on_success = on_success

        self._setup_ui()

    def _setup_ui(self):
        container = ttk.Frame(self, padding=20)
        container.pack(fill=BOTH, expand=YES)

        ttk.Label(
            container,
            text="Compose Message",
            font=("Segoe UI", 12, "bold"),
            bootstyle="primary",
        ).pack(anchor=W, pady=(0, 10))

        self.text_area = ScrolledText(
            container, height=8, autohide=True, font=("Consolas", 10)
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

    def _send(self):
        content = self.text_area.text.get("1.0", END).strip()
        if content:
            # TODO: Get actual user ID/Name if available contextually
            self.board.create_post(999, "Operator", content)
            self.on_success()
            self.destroy()


class ThreadViewDialog(ttk.Toplevel):
    def __init__(self, parent, board, post):
        super().__init__(
            title=f"Secure Channel: {post.author_name}", master=parent
        )
        self.geometry("600x700")
        self.board = board
        self.post = post

        self._setup_ui()

    def _setup_ui(self):
        container = ttk.Frame(self, padding=20)
        container.pack(fill=BOTH, expand=YES)

        # Thread Display Area
        self.text_area = ScrolledText(
            container, autohide=True, state="disabled", font=("Segoe UI", 10)
        )
        self.text_area.pack(fill=BOTH, expand=YES, pady=(0, 20))

        # Define tags for styling
        self.text_area.text.tag_config(
            "author", foreground="#00bc8c", font=("Segoe UI", 10, "bold")
        )
        self.text_area.text.tag_config(
            "root_author", foreground="#f39c12", font=("Segoe UI", 11, "bold")
        )
        self.text_area.text.tag_config("content", foreground="#ffffff")
        self.text_area.text.tag_config(
            "timestamp", foreground="#7f8c8d", font=("Segoe UI", 8)
        )
        self.text_area.text.tag_config("indent", lmargin1=20, lmargin2=20)

        self._render_thread()

        # Reply Section
        reply_frame = ttk.Labelframe(
            container, text="Reply", padding=10, bootstyle="info"
        )
        reply_frame.pack(fill=X)

        self.reply_entry = ttk.Entry(reply_frame, font=("Segoe UI", 10))
        self.reply_entry.pack(side=LEFT, fill=X, expand=YES, padx=(0, 10))
        self.reply_entry.bind("<Return>", lambda e: self._reply())

        ttk.Button(
            reply_frame,
            text="Send Reply",
            bootstyle="info",
            command=self._reply,
        ).pack(side=RIGHT)

    def _render_thread(self):
        self.text_area.text.config(state="normal")
        self.text_area.text.delete("1.0", END)

        # Root Post
        self._insert_post(self.post, is_root=True)

        self.text_area.text.insert(END, "\n" + "-" * 50 + "\n\n", "timestamp")

        # Flattened replies
        self._render_replies(self.post, 1)

        self.text_area.text.config(state="disabled")
        self.text_area.text.see(END)

    def _render_replies(self, parent, level):
        for reply in parent.replies:
            self._insert_post(reply, level=level)
            self._render_replies(reply, level + 1)

    def _insert_post(self, post, is_root=False, level=0):
        indent_tag = f"indent_{level}"
        self.text_area.text.tag_config(
            indent_tag, lmargin1=20 * level, lmargin2=20 * level
        )

        author_tag = "root_author" if is_root else "author"
        ts_str = time.strftime("%H:%M", time.localtime(post.timestamp))

        self.text_area.text.insert(
            END, f"[{ts_str}] ", ("timestamp", indent_tag)
        )
        self.text_area.text.insert(
            END, f"{post.author_name}\n", (author_tag, indent_tag)
        )
        self.text_area.text.insert(
            END, f"{post.content}\n\n", ("content", indent_tag)
        )

    def _reply(self):
        content = self.reply_entry.get().strip()
        if content:
            self.board.create_reply(
                999, "Operator", self.post.message_id, content
            )
            self.board._load_data()
            self.post = self.board.posts.get(self.post.message_id)
            self._render_thread()
            self.reply_entry.delete(0, END)
