#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2014 uralbash <root@uralbash.ru>
#
# Distributed under terms of the MIT license.

"""
SQLAlchemy events extension
"""
# standard library
import weakref

# SQLAlchemy
from sqlalchemy import and_, case, event, select, inspection
from sqlalchemy.orm import object_session
from sqlalchemy.sql import func
from sqlalchemy.orm.base import NO_VALUE



def _insert_subtree(
    table,
    connection,
    node_size,
    node_pos_left,
    node_pos_right,
    parent_pos_left,
    parent_pos_right,
    subtree,
    parent_tree_id,
    parent_level,
    node_level,
    left_sibling,
    right_sibling,
    table_pk,
    audit_id
):
    """

    :param left_sibling: if exists, move node after left sibling
    :param right_sibling: if exists, move node before right sibling
    """
    if left_sibling:
        # step 1: rebuild inserted subtree
        delta_lft = left_sibling["lft"] + 1
        if not left_sibling["is_parent"]:
            delta_lft = left_sibling["rgt"] + 1
        delta_rgt = delta_lft + node_size - 1
        left_limit = left_sibling["lft"]

    elif right_sibling:
        # step 1: rebuild inserted subtree
        delta_lft = right_sibling["lft"]
        delta_rgt = delta_lft + node_size - 1

        left_limit = delta_lft - 1
    else:
        return

    connection.execute(
        table.update(table_pk.in_(subtree)).values(
            lft=table.c.lft - node_pos_left + delta_lft,
            rgt=table.c.rgt - node_pos_right + delta_rgt,
            level=table.c.level - node_level + parent_level + 1,
            tree_id=parent_tree_id,
        )
    )

    # step 2: update key of right side
    condition = [
        table.c.rgt > delta_lft - 1,
        table_pk.notin_(subtree),
        table.c.tree_id == parent_tree_id,
    ]
    if audit_id:
        condition.append(table.c.audit_id == audit_id)
    connection.execute(
        table.update(
            and_(
                *condition
            )
        ).values(
            rgt=table.c.rgt + node_size,
            lft=case(
                [(table.c.lft > left_limit, table.c.lft + node_size)], else_=table.c.lft
            ),
        )
    )


def _get_tree_table(mapper):
    for table in mapper.tables:
        if all(key in table.c for key in ['level', 'lft', 'rgt', 'parent_id']):
            return table


def mptt_before_insert(mapper, connection, instance):
    """ Based on example
    https://bitbucket.org/zzzeek/sqlalchemy/src/73095b353124/examples/nested_sets/nested_sets.py?at=master
    """
    table = _get_tree_table(mapper)
    db_pk = instance.get_pk_column()
    table_pk = getattr(table.c, db_pk.name)

    audit_id = None
    if hasattr(instance, "audit_id"):
        audit_id = instance.audit_id

    if instance.parent_id is None:
        instance.left = 1
        instance.right = 2
        instance.level = instance.get_default_level()
        tree_id = connection.scalar(
            select(
                [
                    func.max(table.c.tree_id) + 1
                ]
            )
        ) or 1
        instance.tree_id = tree_id
    else:
        (parent_pos_left,
         parent_pos_right,
         parent_tree_id,
         parent_level) = connection.execute(
            select(
                [
                    table.c.lft,
                    table.c.rgt,
                    table.c.tree_id,
                    table.c.level
                ]
            ).where(
                table_pk == instance.parent_id
            )
        ).fetchone()

        # Update key of right side
        upd_condition = [
            table.c.rgt >= parent_pos_right,
            table.c.tree_id == parent_tree_id,
        ]
        if audit_id:
            upd_condition.append(table.c.audit_id == audit_id)

        connection.execute(
            table.update(
                and_(*upd_condition)
            ).values(
                lft=case(
                    [
                        (
                            table.c.lft > parent_pos_right,
                            table.c.lft + 2
                        )
                    ],
                    else_=table.c.lft
                ),
                rgt=case(
                    [
                        (
                            table.c.rgt >= parent_pos_right,
                            table.c.rgt + 2
                        )
                    ],
                    else_=table.c.rgt
                )
            )
        )

        instance.level = parent_level + 1
        instance.tree_id = parent_tree_id
        instance.left = parent_pos_right
        instance.right = parent_pos_right + 1


def mptt_before_delete(mapper, connection, instance, delete=True):
    table = _get_tree_table(mapper)
    tree_id = instance.tree_id
    pk = getattr(instance, instance.get_pk_name())
    db_pk = instance.get_pk_column()
    table_pk = getattr(table.c, db_pk.name)
    lft, rgt = connection.execute(
        select(
            [
                table.c.lft,
                table.c.rgt
            ]
        ).where(
            table_pk == pk
        )
    ).fetchone()
    delta = rgt - lft + 1

    audit_id = None
    if hasattr(instance, "audit_id"):
        audit_id = instance.audit_id

    if delete:
        mapper.base_mapper.confirm_deleted_rows = False
        connection.execute(
            table.delete(
                table_pk == pk
            )
        )

    if instance.parent_id is not None or not delete:
        """ Update key of current tree

            UPDATE tree
            SET left_id = CASE
                    WHEN left_id > $leftId THEN left_id - $delta
                    ELSE left_id
                END,
                right_id = CASE
                    WHEN right_id >= $rightId THEN right_id - $delta
                    ELSE right_id
                END
        """

        upd_condition = [
            table.c.rgt > rgt,
            table.c.tree_id == tree_id
        ]
        if audit_id:
            upd_condition.append(table.c.audit_id == audit_id)

        connection.execute(
            table.update(
                and_(*upd_condition)
            ).values(
                lft=case(
                    [
                        (
                            table.c.lft > lft,
                            table.c.lft - delta
                        )
                    ],
                    else_=table.c.lft
                ),
                rgt=case(
                    [
                        (
                            table.c.rgt >= rgt,
                            table.c.rgt - delta
                        )
                    ],
                    else_=table.c.rgt
                )
            )
        )


def mptt_before_update(mapper, connection, instance):
    node_id = getattr(instance, instance.get_pk_name())
    table = _get_tree_table(mapper)
    db_pk = instance.get_pk_column()
    default_level = instance.get_default_level()
    table_pk = getattr(table.c, db_pk.name)
    mptt_move_inside = None
    left_sibling = None
    right_sibling = None
    left_sibling_tree_id = None

    audit_id = None
    if hasattr(instance, "audit_id"):
        audit_id = instance.audit_id

    if hasattr(instance, "mptt_move_inside"):
        mptt_move_inside = instance.mptt_move_inside

    if hasattr(instance, "mptt_move_before"):
        (right_sibling_left, right_sibling_right, right_sibling_parent, right_sibling_tree_id) = (
            connection.execute(
                select([table.c.lft, table.c.rgt, table.c.parent_id, table.c.tree_id]).where(
                    and_(
                        table_pk == instance.mptt_move_before,
                    )
                )
            ).fetchone()
        )

        instance.parent_id = right_sibling_parent
        right_sibling = {"lft": right_sibling_left, "rgt": right_sibling_right, "is_parent": False}

    if hasattr(instance, "mptt_move_after"):
        (left_sibling_left, left_sibling_right, left_sibling_parent, left_sibling_tree_id) = (
            connection.execute(
                select([table.c.lft, table.c.rgt, table.c.parent_id, table.c.tree_id]).where(
                    and_(
                        table_pk == instance.mptt_move_after,
                    )
                )
            ).fetchone()
        )

        instance.parent_id = left_sibling_parent
        left_sibling = {"lft": left_sibling_left, "rgt": left_sibling_right, "is_parent": False}

    where_condition = [
        table.c.lft >= instance.left,
        table.c.rgt <= instance.right,
        table.c.tree_id == instance.tree_id,
    ]
    if audit_id:
        where_condition.append(table.c.audit_id == audit_id)

    subtree = connection.execute(
        select([table_pk])
        .where(
            and_(
                *where_condition
            )
        )
        .order_by(table.c.lft)
    ).fetchall()
    subtree = [x[0] for x in subtree]

    (node_pos_left, node_pos_right, node_tree_id, node_parent_id, node_level) = connection.execute(
        select([table.c.lft, table.c.rgt, table.c.tree_id, table.c.parent_id, table.c.level]).where(
            and_(
                table_pk == node_id,
            )
        )
    ).fetchone()

    if not left_sibling and not right_sibling and str(node_parent_id) == str(instance.parent_id) and not mptt_move_inside:
        if left_sibling_tree_id is None:
            return

    if instance.parent_id is not None:
        (parent_id, parent_pos_right, parent_pos_left, parent_tree_id, parent_level) = (
            connection.execute(
                select([table_pk, table.c.rgt, table.c.lft, table.c.tree_id, table.c.level]).where(
                    and_(
                        table_pk == instance.parent_id,
                    )
                )
            ).fetchone()
        )

        if node_parent_id is None and node_tree_id == parent_tree_id:
            instance.parent_id = None
            return

    mptt_before_delete(mapper, connection, instance, False)

    if instance.parent_id is not None:
        (parent_id, parent_pos_right, parent_pos_left, parent_tree_id, parent_level) = (
            connection.execute(
                select([table_pk, table.c.rgt, table.c.lft, table.c.tree_id, table.c.level]).where(
                    and_(
                        table_pk == instance.parent_id,
                    )
                )
            ).fetchone()
        )

        node_size = node_pos_right - node_pos_left + 1
        if not left_sibling and not right_sibling:
            left_sibling = {"lft": parent_pos_left, "rgt": parent_pos_right, "is_parent": True}
        elif left_sibling and left_sibling["lft"] > node_pos_left:
            left_sibling["lft"] -= node_size
            left_sibling["rgt"] -= node_size
        elif right_sibling and right_sibling["lft"] > node_pos_left:
            right_sibling["lft"] -= node_size
            right_sibling["rgt"] -= node_size

        instance.tree_id = parent_tree_id
        _insert_subtree(
            table,
            connection,
            node_size,
            node_pos_left,
            node_pos_right,
            parent_pos_left,
            parent_pos_right,
            subtree,
            parent_tree_id,
            parent_level,
            node_level,
            left_sibling,
            right_sibling,
            table_pk,
            audit_id
        )
    else:
        if left_sibling_tree_id or left_sibling_tree_id == 0:
            tree_id = left_sibling_tree_id + 1
            connection.execute(
                table.update(table.c.tree_id > left_sibling_tree_id).values(
                    tree_id=table.c.tree_id + 1
                )
            )
        else:
            tree_id = connection.scalar(select([func.max(table.c.tree_id) + 1]))

        connection.execute(
            table.update(table_pk.in_(subtree)).values(
                lft=table.c.lft - node_pos_left + 1,
                rgt=table.c.rgt - node_pos_left + 1,
                level=table.c.level - node_level + default_level,
                tree_id=tree_id,
            )
        )



class _WeakDictBasedSet(weakref.WeakKeyDictionary, object):
    """
    In absence of a default weakset implementation, provide our own dict
    based solution.
    """

    def add(self, obj):
        self[obj] = None

    def discard(self, obj):
        super(_WeakDictBasedSet, self).pop(obj, None)

    def pop(self):
        return self.popitem()[0]


class _WeakDefaultDict(weakref.WeakKeyDictionary, object):

    def __getitem__(self, key):
        try:
            return super(_WeakDefaultDict, self).__getitem__(key)
        except KeyError:
            self[key] = value = _WeakDictBasedSet()
            return value


class TreesManager(object):
    """
    Manages events dispatching for all subclasses of a given class.
    """
    def __init__(self, base_class):
        self.base_class = base_class
        self.classes = set()
        self.instances = _WeakDefaultDict()

    def register_events(self, remove=False):
        for e, h in (
            ('before_insert', self.before_insert),
            ('before_update', self.before_update),
            ('before_delete', self.before_delete),
        ):
            is_event_exist = event.contains(self.base_class, e, h)
            if remove and is_event_exist:
                event.remove(self.base_class, e, h)
            elif not is_event_exist:
                event.listen(self.base_class, e, h, propagate=True)
        return self

    def register_factory(self, sessionmaker):
        """
        Registers this TreesManager instance to respond on
        `after_flush_postexec` events on the given session or session factory.
        This method returns the original argument, so that it can be used by
        wrapping an already existing instance:

        .. code-block:: python
            :linenos:

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker, mapper
            from sqlalchemy_mptt.mixins import BaseNestedSets

            engine = create_engine('...')

            trees_manager = TreesManager(BaseNestedSets)
            trees_manager.register_mapper(mapper)

            Session = tree_manager.register_factory(
                sessionmaker(bind=engine)
            )

        A reference to this method, bound to a default instance of this class
        and already registered to a mapper, is importable directly from
        `sqlalchemy_mptt`:

        .. code-block:: python
            :linenos:

            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            from sqlalchemy_mptt import mptt_sessionmaker

            engine = create_engine('...')
            Session = mptt_sessionmaker(sessionmaker(bind=engine))
        """
        event.listen(sessionmaker, 'after_flush_postexec',
                     self.after_flush_postexec)
        return sessionmaker

    def before_insert(self, mapper, connection, instance):
        session = object_session(instance)
        self.instances[session].add(instance)
        mptt_before_insert(mapper, connection, instance)

    def before_update(self, mapper, connection, instance):
        session = object_session(instance)
        self.instances[session].add(instance)
        mptt_before_update(mapper, connection, instance)

    def before_delete(self, mapper, connection, instance):
        session = object_session(instance)
        self.instances[session].discard(instance)
        mptt_before_delete(mapper, connection, instance)

    def after_flush_postexec(self, session, context):
        """
        Event listener to recursively expire `left` and `right` attributes the
        parents of all modified instances part of this flush.
        """
        instances = self.instances[session]
        while instances:
            instance = instances.pop()
            if instance not in session:
                continue
            parent = self.get_parent_value(instance)

            while parent != NO_VALUE and parent is not None:
                instances.discard(parent)
                session.expire(parent, ['left', 'right', 'tree_id', 'level'])
                parent = self.get_parent_value(parent)
            else:
                session.expire(instance, ['left', 'right', 'tree_id', 'level'])
                self.expire_session_for_children(session, instance)

    @staticmethod
    def get_parent_value(instance):
        return inspection.inspect(instance).attrs.parent.loaded_value

    @staticmethod
    def expire_session_for_children(session, instance):
        children = instance.children

        def expire_recursively(node):
            children = node.children
            for item in children:
                session.expire(item, ['left', 'right', 'tree_id', 'level'])
                expire_recursively(item)

        if children != NO_VALUE and children is not None:
            for item in children:
                session.expire(item, ['left', 'right', 'tree_id', 'level'])
                expire_recursively(item)
