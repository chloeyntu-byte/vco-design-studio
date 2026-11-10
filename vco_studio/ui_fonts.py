"""Product-side font fallback for X servers reporting lowercase family names.

The vendored theme is unchanged. Tk's core-font backend may report a different
capitalization from fontconfig, so match the theme's preferred families without
case sensitivity and derive the product styles from that selected family.
"""
from tkinter import font as tkfont, ttk
from . import fieldwork_tk as fw


def normalize_family_case(root):
    available = {name.casefold(): name for name in tkfont.families(root)}
    family = next((available[name.casefold()] for name in fw._FAMILIES if name.casefold() in available), None)
    if family is None:
        return
    mono = next((available[name.casefold()] for name in fw._MONO if name.casefold() in available), family)
    for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont', 'TkCaptionFont', 'TkSmallCaptionFont', 'TkTooltipFont'):
        tkfont.nametofont(name).configure(family=family)
    tkfont.nametofont('TkFixedFont').configure(family=mono)
    root.fw_fonts = {key: ((mono if key == 'mono' else family),) + value[1:] for key, value in root.fw_fonts.items()}
    style = ttk.Style(root)
    style.configure('.', font=(family, 10))
    for name, size, weight in (('Title.TLabel',14,'bold'),('Num.TLabel',12,'bold'),
                               ('Section.TLabel',9,'bold'),('Primary.TButton',10,'bold'),
                               ('TNotebook.Tab',10,'bold'),('Treeview.Heading',9,'bold')):
        style.configure(name, font=(family, size, weight))
    style.configure('SelectedFamily.TButton', foreground=fw.accent('vco'))
