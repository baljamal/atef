import copy
import json
import logging
import os
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from functools import partial
from typing import (Any, Callable, Generator, Iterable, List, Optional, Tuple,
                    Union, get_args)

import qtawesome as qta
from apischema import ValidationError, deserialize
from qtpy import QtCore, QtWidgets

from atef.config import ConfigurationFile, PreparedFile
from atef.procedure import PreparedProcedureFile, ProcedureFile
from atef.type_hints import PrimitiveType
from atef.widgets.config.utils import TableWidgetWithAddRow
from atef.widgets.core import DesignerDisplay
from atef.widgets.utils import insert_widget

logger = logging.getLogger(__name__)


def walk_find_match(
    item: Any,
    match: Callable,
    parent: List[Tuple[Any, Any]] = []
) -> Generator:
    """
    Walk the dataclass and find every key / field where ``match`` evaluates to True.

    Yields a list of 'paths' to the matching key / field. A path is a list of
    (object, field) tuples that lead from the top level ``item`` to the matching
    key / field.
    - If the object is a dataclass, `field` will be a field in that dataclass
    - If the object is a list, `field` will be the index in that list
    - If the object is a dict, `field` will be a key in that dictionary

    ``match`` should be a Callable taking a single argument and returning a boolean,
    specifying whether that argument matched a search term or not.  This is
    commonly a simple lambda wrapping an equality or regex search.

    Ex:
    paths = walk_find_match(ConfigFile, lambda x: x == 5)
    paths = walk_find_match(ConfigFile, lambda x: re.compile('^warning$').search(x) is not None)

    Parameters
    ----------
    item : Any
        the item to search in.  A dataclass at the top level, but can also be a
        list or dict
    match : Callable
        a function that takes a single argument and returns a boolean
    parent : List[Tuple[Union[str, int], Any]], optional
        the 'path' traveled to arive at ``item`` at this point, by default []
        (used internally)

    Yields
    ------
    List[Tuple[Any, Any]]
        paths leading to keys or fields where ``match`` is True
    """
    if is_dataclass(item):
        # get fields, recurse through fields
        for field in fields(item):
            yield from walk_find_match(getattr(item, field.name), match,
                                       parent=parent + [(item, field.name)])
    elif isinstance(item, list):
        for idx, l_item in enumerate(item):
            # TODO: py3.10 allows isinstance with Unions
            if isinstance(l_item, get_args(PrimitiveType)) and match(l_item):
                yield parent + [('__list__', idx)]
            else:
                yield from walk_find_match(l_item, match,
                                           parent=parent + [('__list__', idx)])
    elif isinstance(item, dict):
        for d_key, d_value in item.items():
            # don't halt at first key match, values could also have matches
            if isinstance(d_value, get_args(PrimitiveType)) and match(d_value):
                yield parent + [('__dictvalue__', d_key)]
            else:
                yield from walk_find_match(d_value, match,
                                           parent=parent + [('__dictvalue__', d_key)])
            if match(d_key):
                yield parent + [('__dictkey__', d_key)]

    elif isinstance(item, Enum):
        if match(item.name):
            yield parent + [('__enum__', item)]

    elif match(item):
        yield parent


def get_deepest_dataclass_in_path(
    path: List[Tuple[Any, Any]],
    item: Optional[Any] = None
) -> Tuple[Any, str]:
    """
    Grab the deepest dataclass in the path, and return its segment

    Parameters
    ----------
    path : List[Tuple[Any, Any]]
        A "path" to a search match, as returned by walk_find_match
    item : Any
        An object to start the path from

    Returns
    -------
    Tuple[AnyDataclass, str]
        The deepest dataclass, and field name for the next step
    """
    rev_idx = -1
    while rev_idx > (-len(path) - 1):
        if is_dataclass(path[rev_idx][0]):
            break
        else:
            rev_idx -= 1
    if item:
        return get_item_from_path(path[:rev_idx], item), path[rev_idx][1]

    return path[rev_idx]


def get_item_from_path(
    path: List[Tuple[Any, Any]],
    item: Optional[Any] = None
) -> Any:
    """
    Get the item the path points to.  This can work for any subpath

    If ``item`` is not provided, use the stashed objects in ``path``.
    Item is expected to be top-level object, if provided.
    (i.e. analagous to path[0][0]).

    Parameters
    ----------
    path : List[Tuple[Any, Any]]
        A "path" to a search match, as returned by walk_find_match
    item : Optional[Any], optional
        the item of interest to explore, by default None

    Returns
    -------
    Any
        the object at the end of ``path``, starting from ``item``
    """
    if not item:
        item = path[0][0]
    for seg in path:
        if seg[0] == '__dictkey__':
            item = seg[1]
        elif seg[0] == '__dictvalue__':
            item = item[seg[1]]
        elif seg[0] == '__list__':
            item = item[seg[1]]
        elif seg[0] == '__enum__':
            item = item.name
        else:
            # general dataclass case
            item = getattr(item, seg[1])
    return item


def replace_item_from_path(
    item: Any,
    path: List[Tuple[Any, Any]],
    replace_fn: Callable
) -> None:
    """
    replace some object in ``item`` located at the end of ``path``, according
    to ``replace_fn``.

    ``replace_fn`` should take the original value, and return the new value
    for insertion into ``item``.  This function frequently involves string
    substitution, and possibly type conversions

    Parameters
    ----------
    item : Any
        The object to replace information in
    path : List[Tuple[Any, Any]]
        A "path" to a search match, as returned by walk_find_match
    replace_fn : Callable
        A function that returns the replacement object
    """
    # need the final step to specify what is being replaced
    final_step = path[-1]
    # need the item one step before the last to perform the assignment on
    parent_item = get_item_from_path(path[:-1], item=item)

    if final_step[0] == "__dictkey__":
        parent_item[replace_fn(final_step[1])] = parent_item.pop(final_step[1])
    elif final_step[0] in ("__dictvalue__", "__list__"):
        # replace value
        old_value = parent_item[final_step[1]]
        parent_item[final_step[1]] = replace_fn(old_value)
    elif final_step[0] == "__enum__":
        parent_item = get_item_from_path(path[:-2], item=item)
        old_enum: Enum = getattr(parent_item, path[-2][1])
        new_enum = getattr(final_step[1], replace_fn(old_enum.name))
        setattr(parent_item, path[-2][1], new_enum)
    else:
        # simple field paths don't have a final (__sth__, ?) segement
        old_value = getattr(parent_item, path[-1][1])
        setattr(parent_item, path[-1][1], replace_fn(old_value))


def get_default_match_fn(search_regex: re.Pattern) -> Callable:
    def match_fn(match):
        return search_regex.search(str(match)) is not None

    return match_fn


def get_default_replace_fn(replace_text: str, search_regex: re.Pattern) -> Callable:
    def replace_fn(value):
        if isinstance(value, str):
            return search_regex.sub(replace_text, value)
        elif isinstance(value, int):
            # cast to float first
            return int(float(value))
        else:  # try to cast as original type
            return type(value)(replace_text)

    return replace_fn


# TODO: consider refactoring an edit into a dataclass?
# (FindReplaceAction with a file, path, replace_fn)?

class FindReplaceWidget(DesignerDisplay, QtWidgets.QWidget):

    search_edit: QtWidgets.QLineEdit
    replace_edit: QtWidgets.QLineEdit

    case_button: QtWidgets.QToolButton
    regex_button: QtWidgets.QToolButton

    preview_button: QtWidgets.QPushButton
    verify_button: QtWidgets.QPushButton
    open_file_button: QtWidgets.QPushButton
    open_converted_button: QtWidgets.QPushButton

    change_list: QtWidgets.QListWidget

    filename = 'find_replace_widget.ui'

    def __init__(
        self,
        *args,
        filepath: Optional[str] = None,
        window: Optional[Any] = None,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.fp = filepath
        self.window = window
        self.match_paths: Iterable[List[Any]] = []
        self.orig_file = None

        if not filepath:
            self.open_converted_button.hide()
        else:
            self.open_file(filename=filepath)

        if window:
            self.open_converted_button.clicked.connect(self.open_converted)
        else:
            self.open_converted_button.hide()

        self.setup_open_file_button()
        self.preview_button.clicked.connect(self.preview_changes)
        self.verify_button.clicked.connect(self.verify_changes)

        self.replace_edit.editingFinished.connect(self.update_replace_fn)
        self.search_edit.editingFinished.connect(self.update_match_fn)
        # placeholder no-op functions
        self._match_fn = lambda x: False
        self._replace_fn = lambda x: x

    def setup_open_file_button(self):
        self.open_file_button.clicked.connect(self.open_file)

    def open_file(self, *args, filename: Optional[str] = None, **kwargs):
        if filename is None:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent=self,
                caption='Select a config',
                filter='Json Files (*.json)',
            )
        if not filename:
            return

        self.fp = filename
        self.orig_file = self.load_file(filename)
        self.setWindowTitle(f'find and replace: ({os.path.basename(filename)})')

    def load_file(self, filepath) -> Union[ConfigurationFile, ProcedureFile]:
        with open(filepath, 'r') as fp:
            self._original_json = json.load(fp)
        try:
            data = deserialize(ConfigurationFile, self._original_json)
        except ValidationError:
            logger.debug('failed to open as passive checkout')
            try:
                data = deserialize(ProcedureFile, self._original_json)
            except ValidationError:
                logger.error('failed to open file as either active '
                             'or passive checkout')

        return data

    def update_replace_fn(self, *args, **kwargs):
        replace_text = self.replace_edit.text()
        replace_fn = get_default_replace_fn(replace_text, self._search_regex)
        self._replace_fn = replace_fn

    def update_match_fn(self, *args, **kwargs):
        search_text = self.search_edit.text()

        flags = re.IGNORECASE if not self.case_button.isChecked() else 0
        use_regex = self.regex_button.isChecked()

        if use_regex:
            self._search_regex = re.compile(f'{search_text}', flags=flags)
        else:
            # exact match
            self._search_regex = re.compile(f'{re.escape(search_text)}', flags=flags)

        match_fn = get_default_match_fn(self._search_regex)
        self._match_fn = match_fn

    def preview_changes(self, *args, **kwargs):
        # update everything to be safe (finishedEditing can be uncertain)
        self.update_match_fn()
        self.update_replace_fn()

        self.change_list.clear()
        self.match_paths = walk_find_match(self.orig_file, self._match_fn)
        replace_text = self.replace_edit.text()
        search_text = self.search_edit.text()

        def remove_item(list_item):
            self.change_list.takeItem(self.change_list.row(list_item))

        def accept_change(list_item):
            try:
                replace_item_from_path(self.orig_file, path,
                                       replace_fn=self._replace_fn)
            except KeyError:
                logger.warning(f'Unable to replace ({search_text}) with '
                               f'({replace_text}) in file.  File may have '
                               f'already been edited')
            except Exception as ex:
                logger.warning(f'Unable to apply change. {ex}')

            remove_item(list_item)

        # generator can be unstable if dataclass changes during walk
        # this is only ok because we consume generator entirely
        for path in self.match_paths:
            # Modify a preview file to create preview
            preview_file = self.load_file(self.fp)
            if replace_text:
                try:
                    replace_item_from_path(preview_file, path,
                                           replace_fn=self._replace_fn)
                    post_text = str(get_item_from_path(path[:-1], item=preview_file))
                except Exception as ex:
                    logger.warning('Unable to generate preview, provided replacement '
                                   f'text is invalid: {ex}')
                    post_text = '[INVALID]'
            else:
                post_text = ''

            pre_text = str(get_item_from_path(path[:-1], item=self.orig_file))
            row_widget = FindReplaceRow(pre_text=pre_text,
                                        post_text=post_text,
                                        path=path)

            l_item = QtWidgets.QListWidgetItem()
            l_item.setSizeHint(QtCore.QSize(row_widget.width(), row_widget.height()))
            self.change_list.addItem(l_item)
            self.change_list.setItemWidget(l_item, row_widget)

            row_widget.button_box.accepted.connect(partial(accept_change, l_item))
            row_widget.button_box.rejected.connect(partial(remove_item, l_item))

    def verify_changes(self, *args, **kwargs):
        # check to make sure changes are valid

        try:
            if self.config_type is ConfigurationFile:
                self.prepared_file = PreparedFile.from_config(self.orig_file)
            if self.config_type is ProcedureFile:
                # clear all results when making a new run tree
                self.prepared_file = PreparedProcedureFile.from_origin(self.orig_file)
        except Exception as ex:
            print(f'prepare fail: {ex}')
            return

        print('should work')

    def open_converted(self, *args, **kwargs):
        # open new file in new tab
        self.window._new_tab(data=self.orig_file, filename=self.fp)


class FindReplaceRow(DesignerDisplay, QtWidgets.QWidget):

    button_box: QtWidgets.QDialogButtonBox
    dclass_label: QtWidgets.QLabel
    pre_label: QtWidgets.QLabel
    post_label: QtWidgets.QLabel
    details_button: QtWidgets.QToolButton

    filename = 'find_replace_row_widget.ui'

    def __init__(
        self,
        *args,
        pre_text: str = 'pre',
        post_text: str = 'post',
        path: List[Any] = [],
        **kwargs
    ) -> None:
        super().__init__(*args, *kwargs)
        last_dclass, attr = get_deepest_dataclass_in_path(path)
        dclass_type = type(last_dclass).__name__

        self.dclass_label.setText(f'{dclass_type}.{attr}')
        self.pre_label.setText(pre_text)
        self.pre_label.setToolTip(pre_text)
        self.post_label.setText(post_text)
        self.post_label.setToolTip(post_text)

        self.button_box.button(QtWidgets.QDialogButtonBox.Ok).setText('')
        self.button_box.button(QtWidgets.QDialogButtonBox.Cancel).setText('')

        path_list = []
        for segment in path:
            if isinstance(segment[0], str):
                name = segment[0]
            else:
                name = type(segment[0]).__name__
            path_list.append(f'({name}, {segment[1]})')

        path_str = '>'.join(path_list)
        detail_widget = QtWidgets.QLabel(path_str + '\n')
        detail_widget.setWordWrap(True)

        widget_action = QtWidgets.QWidgetAction(self.details_button)
        widget_action.setDefaultWidget(detail_widget)

        widget_menu = QtWidgets.QMenu(self.details_button)
        widget_menu.addAction(widget_action)
        self.details_button.setMenu(widget_menu)


class FillTemplatePage(DesignerDisplay, QtWidgets.QWidget):

    file_name_label: QtWidgets.QLabel
    # update with * when unsaved,
    type_label: QtWidgets.QLabel

    details_list: QtWidgets.QListWidget
    # show list of edits (find_replace_rows) depending on selected edit
    devices_list: QtWidgets.QListWidget
    # scan through devices in original checkout?
    # filter by device type?  look at specific device types?
    edits_table: TableWidgetWithAddRow
    edits_table_placeholder: QtWidgets.QWidget
    # possibly a table widget?  starting device / string, happi selector for replace
    # go button for calculation, refresh on select

    apply_all_button: QtWidgets.QPushButton
    # update with number of unsaved changes
    open_button: QtWidgets.QPushButton
    save_button: QtWidgets.QPushButton
    # open message box for save-as functionality

    filename = 'fill_template_page.ui'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orig_file = None
        self.setup_ui()

    def setup_ui(self):
        self.open_button.clicked.connect(self.open_file)
        self.save_button.clicked.connect(self.save_file)
        self.apply_all_button.clicked.connect(self.apply_all)

        self.setup_edits_table()

    def setup_edits_table(self):
        # set up add row widget for edits
        self.edits_table = TableWidgetWithAddRow(
            add_row_text='add edit', title_text='edits',
            row_widget_cls=partial(TemplateEditRowWidget, orig_file=self.orig_file)
        )
        insert_widget(self.edits_table, self.edits_table_placeholder)
        self.edits_table.table_updated.connect(
            self.update_change_list
        )
        # connect an update-change-list slot to edits_table.table_updated
        self.edits_table.itemSelectionChanged.connect(self.show_changes_from_edit)
        self.edits_table.setSelectionMode(self.edits_table.SingleSelection)

    def open_file(self, *args, filename: Optional[str] = None, **kwargs):
        # open file with message box
        if filename is None:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent=self,
                caption='Select a config',
                filter='Json Files (*.json)',
            )
        if not filename:
            return

        # update title label
        self.fp = filename
        with open(self.fp, 'r') as fp:
            self._original_json = json.load(fp)
        try:
            self.orig_file = deserialize(ConfigurationFile, self._original_json)
        except ValidationError:
            logger.debug('failed to open as passive checkout')
            try:
                self.orig_file = deserialize(ProcedureFile, self._original_json)
            except ValidationError:
                logger.error('failed to open file as either active '
                             'or passive checkout')
        self.setup_edits_table()
        self.update_title()

    def save_file(self):
        # open save message box
        self.prompt_apply()
        raise NotImplementedError

    def apply_all(self):
        self.prompt_apply()
        self.update_title()
        raise NotImplementedError

    def prompt_apply(self):
        # message box with details on remaining changes
        raise NotImplementedError

    def update_title(self):
        file_name = os.path.basename(self.fp)
        if len(self.remaining_changes()) > 0:
            file_name += '*'
        # if edited (items in change list), add *
        # update type
        # update tab title?
        self.file_name_label.setText(file_name)
        self.type_label.setText(type(self.orig_file).__name__)

    def update_change_list(self):
        # walk through edits_table, gather list of list of paths
        # store total count
        raise NotImplementedError

    def show_changes_from_edit(self, *args, **kwargs):
        self.details_list.clear()
        # on selected callback, populate details table
        selected_range = self.edits_table.selectedRanges()[0]
        edit_row_widget: TemplateEditRowWidget = self.edits_table.cellWidget(
            selected_range.topRow(), 0
        )
        print(selected_range.topRow(), edit_row_widget)
        if not isinstance(edit_row_widget, TemplateEditRowWidget):
            return
        # use FindRowWidgets
        for row_widget in edit_row_widget.get_details_rows():
            def remove_item(list_item):
                self.change_list.takeItem(self.change_list.row(list_item))

            l_item = QtWidgets.QListWidgetItem()
            l_item.setSizeHint(QtCore.QSize(row_widget.width(), row_widget.height()))
            self.details_list.addItem(l_item)
            self.details_list.setItemWidget(l_item, row_widget)
            row_widget.button_box.accepted.connect(remove_item)
            row_widget.button_box.rejected.connect(remove_item)

    def get_changes_from_edit(self, replace_fn, path):
        # create match fn, replace fn from row information
        # stash a change list
        # (to be called on selection and on go-button)
        raise NotImplementedError


class TemplateEditRowWidget(DesignerDisplay, QtWidgets.QWidget):
    button_box: QtWidgets.QDialogButtonBox
    child_button: QtWidgets.QPushButton

    setting_button: QtWidgets.QToolButton
    regex_button: QtWidgets.QToolButton
    case_button: QtWidgets.QToolButton

    search_edit: QtWidgets.QLineEdit
    replace_edit: QtWidgets.QLineEdit

    filename = 'template_edit_row_widget.ui'

    def __init__(self, *args, data=None, orig_file: Union[ConfigurationFile, ProcedureFile], **kwargs):
        # Expected SimpleRowWidgets are DataWidgets, expecting a dataclass
        super().__init__(*args, **kwargs)
        self.orig_file = orig_file
        self.match_paths: Iterable[List[Any]] = []
        self.details_rows: List[FindReplaceRow] = []
        self.setup_ui()

    def setup_ui(self):
        self.child_button.hide()

        # self.button_box.button(QtWidgets.QDialogButtonBox.Apply).setText('')
        # self.button_box.button(QtWidgets.QDialogButtonBox.Cancel).setText('')
        self.button_box.button(QtWidgets.QDialogButtonBox.Retry).clicked.connect(self.refresh_paths)
        # settings menu (regex, case)
        self.setting_widget = QtWidgets.QWidget()
        self.setting_layout = QtWidgets.QHBoxLayout()
        self.regex_button = QtWidgets.QToolButton()
        self.regex_button.setCheckable(True)
        self.regex_button.setText('.*')
        self.regex_button.setToolTip('use regex')
        self.case_button = QtWidgets.QToolButton()
        self.case_button.setCheckable(True)
        self.case_button.setText('Aa')
        self.case_button.setToolTip('case sensitive')
        self.setting_layout.addWidget(self.regex_button)
        self.setting_layout.addWidget(self.case_button)
        self.setting_widget.setLayout(self.setting_layout)
        widget_action = QtWidgets.QWidgetAction(self.setting_button)
        widget_action.setDefaultWidget(self.setting_widget)

        widget_menu = QtWidgets.QMenu(self.setting_button)
        widget_menu.addAction(widget_action)
        self.setting_button.setMenu(widget_menu)
        self.setting_button.setIcon(qta.icon('fa.gear'))

    def update_replace_fn(self, *args, **kwargs):
        replace_text = self.replace_edit.text()
        replace_fn = get_default_replace_fn(replace_text, self._search_regex)
        self._replace_fn = replace_fn

    def update_match_fn(self, *args, **kwargs):
        search_text = self.search_edit.text()

        flags = re.IGNORECASE if not self.case_button.isChecked() else 0
        use_regex = self.regex_button.isChecked()

        if use_regex:
            self._search_regex = re.compile(f'{search_text}', flags=flags)
        else:
            # exact match
            self._search_regex = re.compile(f'{re.escape(search_text)}',
                                            flags=flags)

        match_fn = get_default_match_fn(self._search_regex)
        self._match_fn = match_fn

    def refresh_paths(self):
        print(f'refresh_paths, {type(self.orig_file)}')
        if self.orig_file is None:
            return
        # update everything to be safe (finishedEditing can be uncertain)
        self.update_match_fn()
        self.update_replace_fn()

        self.details_rows.clear()
        self.match_paths = walk_find_match(self.orig_file, self._match_fn)
        replace_text = self.replace_edit.text()
        search_text = self.search_edit.text()

        # generator can be unstable if dataclass changes during walk
        # this is only ok because we consume generator entirely
        for path in self.match_paths:
            # Modify a preview file to create preview
            preview_file = copy.deepcopy(self.orig_file)
            if replace_text:
                try:
                    replace_item_from_path(preview_file, path,
                                           replace_fn=self._replace_fn)
                    post_text = str(get_item_from_path(path[:-1], item=preview_file))
                except Exception as ex:
                    logger.warning('Unable to generate preview, provided replacement '
                                   f'text is invalid: {ex}')
                    post_text = '[INVALID]'
            else:
                post_text = ''

            def accept_change():
                try:
                    replace_item_from_path(self.orig_file, path,
                                           replace_fn=self._replace_fn)
                except KeyError:
                    logger.warning(f'Unable to replace ({search_text}) with '
                                   f'({replace_text}) in file.  File may have '
                                   f'already been edited')
                except Exception as ex:
                    logger.warning(f'Unable to apply change. {ex}')

                print(len(self.details_rows))

            pre_text = str(get_item_from_path(path[:-1], item=self.orig_file))
            row_widget = FindReplaceRow(pre_text=pre_text,
                                        post_text=post_text,
                                        path=path)
            row_widget.button_box.accepted.connect(accept_change)

            self.details_rows.append(row_widget)

    def get_details_rows(self) -> List[FindReplaceRow]:
        return self.details_rows
