"""
A custom manager for working with trees of objects.
"""
import contextlib
import functools
import uuid
from collections import defaultdict
from itertools import groupby

from django.db import connections, models, router
from django.db.models import (
    F,
    IntegerField,
    ManyToManyField,
    Max,
    OuterRef,
    Q,
    Subquery,
)
from django.utils.translation import gettext as _

from mptt.compat import cached_field_value
from mptt.exceptions import CantDisableUpdates, InvalidMove
from mptt.querysets import TreeQuerySet
from mptt.signals import node_moved
from mptt.utils import _get_tree_model, clean_tree_ids

__all__ = ("TreeManager",)


class SQCount(Subquery):
    template = "(SELECT count(*) FROM (%(subquery)s) _count)"
    output_field = IntegerField()


def delegate_manager(method):
    """
    Delegate method calls to base manager, if exists.
    """

    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._base_manager:
            return getattr(self._base_manager, method.__name__)(*args, **kwargs)
        return method(self, *args, **kwargs)

    return wrapped


class TreeManager(models.Manager.from_queryset(TreeQuerySet)):

    """
    A manager for working with trees of objects.
    """

    def contribute_to_class(self, model, name):
        super().contribute_to_class(model, name)

        if not model._meta.abstract:
            self.tree_model = _get_tree_model(model)

            self._base_manager = None
            if self.tree_model and self.tree_model is not model:
                # _base_manager is the treemanager on tree_model
                self._base_manager = self.tree_model._tree_manager

    def get_queryset(self, *args, **kwargs):
        """
        Ensures that this manager always returns nodes in tree order.
        """
        return (
            super()
            .get_queryset(*args, **kwargs)
            .order_by(self.tree_id_attr, self.left_attr)
        )

    def _get_queryset_relatives(self, queryset, direction, include_self):
        """
        Returns a queryset containing either the descendants
        ``direction == desc`` or the ancestors ``direction == asc`` of a given
        queryset.

        This function is not meant to be called directly, although there is no
        harm in doing so.

        Instead, it should be used via ``get_queryset_descendants()`` and/or
        ``get_queryset_ancestors()``.

        This function works by grouping contiguous siblings and using them to create
        a range that selects all nodes between the range, instead of querying for each
        node individually. Three variables are required when querying for ancestors or
        descendants: tree_id_attr, left_attr, right_attr. If we weren't using ranges
        and our queryset contained 100 results, the resulting SQL query would contain
        300 variables. However, when using ranges, if the same queryset contained 10
        sets of contiguous siblings, then the resulting SQL query should only contain
        30 variables.

        The attributes used to create the range are completely
        dependent upon whether you are ascending or descending the tree.

        * Ascending (ancestor nodes): select all nodes whose right_attr is greater
          than (or equal to, if include_self = True) the smallest right_attr within
          the set of contiguous siblings, and whose left_attr is less than (or equal
          to) the largest left_attr within the set of contiguous siblings.

        * Descending (descendant nodes): select all nodes whose left_attr is greater
          than (or equal to, if include_self = True) the smallest left_attr within
          the set of contiguous siblings, and whose right_attr is less than (or equal
          to) the largest right_attr within the set of contiguous siblings.

        The result is the more contiguous siblings in the original queryset, the fewer
        SQL variables will be required to execute the query.
        """
        assert self.model is queryset.model

        opts = queryset.model._mptt_meta

        filters = Q()

        e = "e" if include_self else ""
        max_op = "lt" + e
        min_op = "gt" + e
        if direction == "asc":
            max_attr = opts.left_attr
            min_attr = opts.right_attr
        elif direction == "desc":
            max_attr = opts.right_attr
            min_attr = opts.left_attr

        tree_key = opts.tree_id_attr
        min_key = f"{min_attr}__{min_op}"
        max_key = f"{max_attr}__{max_op}"

        q = queryset.order_by(opts.tree_id_attr, opts.parent_attr, opts.left_attr).only(
            opts.tree_id_attr,
            opts.left_attr,
            opts.right_attr,
            min_attr,
            max_attr,
            opts.parent_attr,
            # These fields are used by MPTTModel.update_mptt_cached_fields()
            *[f.lstrip("-") for f in opts.order_insertion_by],
        )

        if not q:
            return self.none()

        for group in groupby(
            q,
            key=lambda n: (
                getattr(n, opts.tree_id_attr),
                getattr(n, opts.parent_attr + "_id"),
            ),
        ):
            next_lft = None
            for node in list(group[1]):
                tree, lft, rght, min_val, max_val = (
                    getattr(node, opts.tree_id_attr),
                    getattr(node, opts.left_attr),
                    getattr(node, opts.right_attr),
                    getattr(node, min_attr),
                    getattr(node, max_attr),
                )
                if next_lft is None:
                    next_lft = rght + 1
                    min_max = {"min": min_val, "max": max_val}
                elif lft == next_lft:
                    if min_val < min_max["min"]:
                        min_max["min"] = min_val
                    if max_val > min_max["max"]:
                        min_max["max"] = max_val
                    next_lft = rght + 1
                elif lft != next_lft:
                    filters |= Q(
                        **{
                            tree_key: tree,
                            min_key: min_max["min"],
                            max_key: min_max["max"],
                        }
                    )
                    min_max = {"min": min_val, "max": max_val}
                    next_lft = rght + 1
            filters |= Q(
                **{
                    tree_key: tree,
                    min_key: min_max["min"],
                    max_key: min_max["max"],
                }
            )

        return self.filter(filters)

    def get_queryset_descendants(self, queryset, include_self=False):
        """
        Returns a queryset containing the descendants of all nodes in the
        given queryset.

        If ``include_self=True``, nodes in ``queryset`` will also
        be included in the result.
        """
        return self._get_queryset_relatives(queryset, "desc", include_self)

    def get_queryset_ancestors(self, queryset, include_self=False):
        """
        Returns a queryset containing the ancestors
        of all nodes in the given queryset.

        If ``include_self=True``, nodes in ``queryset`` will also
        be included in the result.
        """
        return self._get_queryset_relatives(queryset, "asc", include_self)

    @contextlib.contextmanager
    def disable_mptt_updates(self):
        """
        Context manager. Disables mptt updates.

        NOTE that this context manager causes inconsistencies! MPTT model
        methods are not guaranteed to return the correct results.

        When to use this method:
            If used correctly, this method can be used to speed up bulk
            updates.

            This doesn't do anything clever. It *will* mess up your tree.  You
            should follow this method with a call to ``TreeManager.rebuild()``
            to ensure your tree stays sane, and you should wrap both calls in a
            transaction.

            This is best for updates that span a large part of the table.  If
            you are doing localised changes (one tree, or a few trees) consider
            using ``delay_mptt_updates``.

            If you are making only minor changes to your tree, just let the
            updates happen.

        Transactions:
            This doesn't enforce any transactional behavior.  You should wrap
            this in a transaction to ensure database consistency.

        If updates are already disabled on the model, this is a noop.

        Usage::

            with transaction.atomic():
                with MyNode.objects.disable_mptt_updates():
                    ## bulk updates.
                MyNode.objects.rebuild()
        """
        # Error cases:
        if self.model._meta.abstract:
            #  an abstract model. Design decision needed - do we disable
            #  updates for all concrete models that derive from this model?  I
            #  vote no - that's a bit implicit and it's a weird use-case
            #  anyway.  Open to further discussion :)
            raise CantDisableUpdates(
                "You can't disable/delay mptt updates on %s,"
                " it's an abstract model" % self.model.__name__
            )
        elif self.model._meta.proxy:
            #  a proxy model. disabling updates would implicitly affect other
            #  models using the db table. Caller should call this on the
            #  manager for the concrete model instead, to make the behavior
            #  explicit.
            raise CantDisableUpdates(
                "You can't disable/delay mptt updates on %s, it's a proxy"
                " model. Call the concrete model instead." % self.model.__name__
            )
        elif self.tree_model is not self.model:
            #  a multiple-inheritance child of an MPTTModel.  Disabling
            #  updates may affect instances of other models in the tree.
            raise CantDisableUpdates(
                "You can't disable/delay mptt updates on %s, it doesn't"
                " contain the mptt fields." % self.model.__name__
            )

        if not self.model._mptt_updates_enabled:
            # already disabled, noop.
            yield
        else:
            self.model._set_mptt_updates_enabled(False)
            try:
                yield
            finally:
                self.model._set_mptt_updates_enabled(True)

    @contextlib.contextmanager
    def delay_mptt_updates(self):
        """
        Context manager. Delays mptt updates until the end of a block of bulk
        processing.

        NOTE that this context manager causes inconsistencies! MPTT model
        methods are not guaranteed to return the correct results until the end
        of the context block.

        When to use this method:
            If used correctly, this method can be used to speed up bulk
            updates.  This is best for updates in a localised area of the db
            table, especially if all the updates happen in a single tree and
            the rest of the forest is left untouched.  No subsequent rebuild is
            necessary.

            ``delay_mptt_updates`` does a partial rebuild of the modified trees
            (not the whole table).  If used indiscriminately, this can actually
            be much slower than just letting the updates occur when they're
            required.

            The worst case occurs when every tree in the table is modified just
            once.  That results in a full rebuild of the table, which can be
            *very* slow.

            If your updates will modify most of the trees in the table (not a
            small number of trees), you should consider using
            ``TreeManager.disable_mptt_updates``, as it does much fewer
            queries.

        Transactions:
            This doesn't enforce any transactional behavior.  You should wrap
            this in a transaction to ensure database consistency.

        Exceptions:
            If an exception occurs before the processing of the block, delayed
            updates will not be applied.

        Usage::

            with transaction.atomic():
                with MyNode.objects.delay_mptt_updates():
                    ## bulk updates.
        """
        with self.disable_mptt_updates():
            if self.model._mptt_is_tracking:
                # already tracking, noop.
                yield
            else:
                self.model._mptt_start_tracking()
                try:
                    yield
                except Exception:
                    # stop tracking, but discard results
                    self.model._mptt_stop_tracking()
                    raise
                results = self.model._mptt_stop_tracking()
                partial_rebuild = self.partial_rebuild
                for tree_id in results:
                    partial_rebuild(tree_id)

    @property
    def parent_attr(self):
        return self.model._mptt_meta.parent_attr

    @property
    def left_attr(self):
        return self.model._mptt_meta.left_attr

    @property
    def right_attr(self):
        return self.model._mptt_meta.right_attr

    @property
    def tree_id_attr(self):
        return self.model._mptt_meta.tree_id_attr

    @property
    def level_attr(self):
        return self.model._mptt_meta.level_attr

    def _translate_lookups(self, **lookups):
        new_lookups = {}
        join_parts = "__".join
        for k, v in lookups.items():
            parts = k.split("__")
            new_parts = []
            new_parts__append = new_parts.append
            for part in parts:
                new_parts__append(getattr(self, part + "_attr", part))
            new_lookups[join_parts(new_parts)] = v
        return new_lookups

    @delegate_manager
    def _mptt_filter(self, qs=None, **filters):
        """
        Like ``self.filter()``, but translates name-agnostic filters for MPTT
        fields.
        """
        if qs is None:
            qs = self
        return qs.filter(**self._translate_lookups(**filters))

    @delegate_manager
    def _mptt_update(self, qs=None, **items):
        """
        Like ``self.update()``, but translates name-agnostic MPTT fields.
        """
        if qs is None:
            qs = self
        return qs.update(**self._translate_lookups(**items))

    def _get_connection(self, **hints):
        return connections[router.db_for_write(self.model, **hints)]

    def add_related_count(
        self,
        queryset,
        rel_model,
        rel_field,
        count_attr,
        cumulative=False,
        extra_filters=None,
    ):
        """
        Adds a related item count to a given ``QuerySet`` using its
        ``extra`` method, for a ``Model`` class which has a relation to
        this ``Manager``'s ``Model`` class.

        Arguments:

        ``rel_model``
           A ``Model`` class which has a relation to this `Manager``'s
           ``Model`` class.

        ``rel_field``
           The name of the field in ``rel_model`` which holds the
           relation.

        ``count_attr``
           The name of an attribute which should be added to each item in
           this ``QuerySet``, containing a count of how many instances
           of ``rel_model`` are related to it through ``rel_field``.

        ``cumulative``
           If ``True``, the count will be for each item and all of its
           descendants, otherwise it will be for each item itself.

        ``extra_filters``
           Dict with additional parameters filtering the related queryset.
        """
        if extra_filters is None:
            extra_filters = {}
        if cumulative:
            subquery_filters = {
                rel_field + "__tree_id": OuterRef(self.tree_id_attr),
                rel_field + "__lft__gte": OuterRef(self.left_attr),
                rel_field + "__lft__lte": OuterRef(self.right_attr),
            }
        else:
            current_rel_model = rel_model
            for rel_field_part in rel_field.split("__"):
                current_mptt_field = current_rel_model._meta.get_field(rel_field_part)
                current_rel_model = current_mptt_field.related_model
            mptt_field = current_mptt_field

            if isinstance(mptt_field, ManyToManyField):
                field_name = "pk"
            else:
                field_name = mptt_field.remote_field.field_name

            subquery_filters = {
                rel_field: OuterRef(field_name),
            }
        subquery = rel_model.objects.filter(**subquery_filters, **extra_filters).values(
            "pk"
        )
        return queryset.annotate(**{count_attr: SQCount(subquery)})

    @delegate_manager
    def insert_node(
        self,
        node,
        target,
        position="last-child",
        save=False,
        allow_existing_pk=False,
        refresh_target=True,
    ):
        """
        Sets up the tree state for ``node`` (which has not yet been
        inserted into in the database) so it will be positioned relative
        to a given ``target`` node as specified by ``position`` (when
        appropriate) it is inserted, with any necessary space already
        having been made for it.

        A ``target`` of ``None`` indicates that ``node`` should be
        the last root node.

        If ``save`` is ``True``, ``node``'s ``save()`` method will be
        called before it is returned.

        NOTE: This is a low-level method; it does NOT respect
        ``MPTTMeta.order_insertion_by``.  In most cases you should just
        set the node's parent and let mptt call this during save.
        """

        root_node_ordering = self.model._mptt_meta.root_node_ordering

        if node.pk and not allow_existing_pk and self.filter(pk=node.pk).exists():
            raise ValueError(_("Cannot insert a node which has already been saved."))

        if target is None:
            tree_id = self._get_next_tree_id()
            setattr(node, self.left_attr, 1)
            setattr(node, self.right_attr, 2)
            setattr(node, self.level_attr, 0)
            setattr(node, self.tree_id_attr, tree_id)
            setattr(node, self.parent_attr, None)
        elif target.is_root_node() and position in ["left", "right"]:
            if refresh_target:
                # Ensure mptt values on target are not stale.
                target._mptt_refresh()

            target_tree_id = getattr(target, self.tree_id_attr)
            if position == "left":
                tree_id = target_tree_id
                space_target = target_tree_id - 1 if root_node_ordering else self._get_next_tree_id()
            else:
                tree_id = target_tree_id + 1 if root_node_ordering else self._get_next_tree_id()
                space_target = target_tree_id
            if root_node_ordering:
                self._create_tree_space(space_target)

            setattr(node, self.left_attr, 1)
            setattr(node, self.right_attr, 2)
            setattr(node, self.level_attr, 0)
            setattr(node, self.tree_id_attr, tree_id)
            setattr(node, self.parent_attr, None)
        else:
            setattr(node, self.left_attr, 0)
            setattr(node, self.level_attr, 0)

            if refresh_target:
                # Ensure mptt values on target are not stale.
                target._mptt_refresh()

            (
                space_target,
                level,
                left,
                parent,
                right_shift,
            ) = self._calculate_inter_tree_move_values(node, target, position)

            tree_id = getattr(target, self.tree_id_attr)
            self._create_space(2, space_target, tree_id)

            setattr(node, self.left_attr, -left)
            setattr(node, self.right_attr, -left + 1)
            setattr(node, self.level_attr, -level)
            setattr(node, self.tree_id_attr, tree_id)
            setattr(node, self.parent_attr, parent)

            if parent:
                self._post_insert_update_cached_parent_right(parent, right_shift)

        if save:
            node.save()
        return node

    @delegate_manager
    def _move_node(
        self, node, target, position="last-child", save=True, refresh_target=True
    ):
        if self.tree_model._mptt_is_tracking:
            # delegate to insert_node and clean up the gaps later.
            return self.insert_node(
                node,
                target,
                position=position,
                save=save,
                allow_existing_pk=True,
                refresh_target=refresh_target,
            )
        else:
            if target is None:
                if node.is_child_node():
                    self._make_child_root_node(node)
            elif target.is_root_node() and position in ("left", "right"):
                self._make_sibling_of_root_node(node, target, position)
            else:
                if node.is_root_node():
                    self._move_root_node(node, target, position)
                else:
                    self._move_child_node(node, target, position)

    def move_node(self, node, target, position="last-child"):
        """
        Moves ``node`` relative to a given ``target`` node as specified
        by ``position`` (when appropriate), by examining both nodes and
        calling the appropriate method to perform the move.

        A ``target`` of ``None`` indicates that ``node`` should be
        turned into a root node.

        Valid values for ``position`` are ``'first-child'``,
        ``'last-child'``, ``'left'`` or ``'right'``.

        ``node`` will be modified to reflect its new tree state in the
        database.

        This method explicitly checks for ``node`` being made a sibling
        of a root node, as this is a special case due to our use of tree
        ids to order root nodes.

        NOTE: This is a low-level method; it does NOT respect
        ``MPTTMeta.order_insertion_by``.  In most cases you should just
        move the node yourself by setting node.parent.
        """
        self._move_node(node, target, position=position)
        node.save()
        node_moved.send(
            sender=node.__class__, instance=node, target=target, position=position
        )

    @delegate_manager
    def root_node(self, tree_id):
        """
        Returns the root node of the tree with the given id.
        """
        return self._mptt_filter(tree_id=tree_id, parent=None).get()

    @delegate_manager
    def root_nodes(self):
        """
        Creates a ``QuerySet`` containing root nodes.
        """
        return self._mptt_filter(parent=None)

    def _find_out_rebuild_fields(self):
        """
        Due to the behavior of the metaclass, it is not possible
        to find out the fields in the __init__ correctly.
        """
        lookups = self._translate_lookups(
            left="left", right="right", level="level", tree_id="tree_id"
        )
        self._rebuild_fields = {value: key for key, value in lookups.items()}

    def _get_parents(self, **filters):
        opts = self.model._mptt_meta
        qs = self._mptt_filter(parent=None, **filters)
        if opts.order_insertion_by:
            qs = qs.order_by(*opts.order_insertion_by)
        return list(qs.only("pk"))

    def _get_children(self, **filters):
        opts = self.model._mptt_meta
        qs = self._mptt_filter(parent__isnull=False, **filters)
        if opts.order_insertion_by:
            qs = qs.order_by(*opts.order_insertion_by)

        children = defaultdict(list)
        for child in qs.select_related(opts.parent_attr):
            children[getattr(child, opts.parent_attr).pk].append(child)
        return children

    @delegate_manager
    def rebuild(self, batch_size=1000, **filters) -> None:
        """
        Rebuilds all trees in the database table using `parent` link.
        """
        self._find_out_rebuild_fields()

        parents = self._get_parents(**filters)
        children = self._get_children(**filters)

        # forked modification
        tree_id = filters.get("tree_id", 1)
        nodes_to_update = []
        for index, parent in enumerate(parents):
            self._rebuild_helper(
                node=parent,
                left=1,
                tree_id=tree_id + index if self.model._mptt_meta.root_node_ordering else uuid.uuid4(),
                children=children,
                nodes_to_update=nodes_to_update,
                level=0,
            )
        self.bulk_update(
            nodes_to_update,
            self._rebuild_fields.values(),
            batch_size=batch_size,
        )

    rebuild.alters_data = True

    def _rebuild_helper(self, node, left, tree_id, children, nodes_to_update, level):
        right = left + 1

        for child in children[node.pk]:
            right = self._rebuild_helper(
                node=child,
                left=right,
                tree_id=tree_id,
                children=children,
                nodes_to_update=nodes_to_update,
                level=level + 1,
            )

        setattr(node, self._rebuild_fields["left"], left)
        setattr(node, self._rebuild_fields["right"], right)
        setattr(node, self._rebuild_fields["level"], level)
        setattr(node, self._rebuild_fields["tree_id"], tree_id)
        nodes_to_update.append(node)

        return right + 1

    @delegate_manager
    def partial_rebuild(self, tree_id, batch_size=1000, **filters):
        """
        Partially rebuilds a tree i.e. It rebuilds only the tree with given
        ``tree_id`` in database table using ``parent`` link.
        """
        count = self._mptt_filter(parent=None, tree_id=tree_id, **filters).count()

        if count == 0:
            return
        elif count == 1:
            self.rebuild(batch_size=batch_size, tree_id=tree_id, **filters)
        else:
            raise RuntimeError(
                "More than one root node with tree_id %d. That's invalid,"
                " do a full rebuild." % tree_id
            )

    @delegate_manager
    def build_tree_nodes(self, data, target=None, position="last-child"):
        """
        Load a tree from a nested dictionary for bulk insert, returning an
        array of records. Use to efficiently insert many nodes within a tree
        without an expensive `rebuild`.

        ::

            records = MyModel.objects.build_tree_nodes({
                'id': 7,
                'name': 'parent',
                'children': [
                    {
                        'id': 8,
                        'parent_id': 7,
                        'name': 'child',
                        'children': [
                            {
                                'id': 9,
                                'parent_id': 8,
                                'name': 'grandchild',
                            }
                        ]
                    }
                ]
            })
            MyModel.objects.bulk_create(records)

        """
        opts = self.model._mptt_meta
        if target:
            tree_id = target.tree_id
            if position in ("left", "right"):
                level = getattr(target, opts.level_attr)
                if position == "left":
                    cursor = getattr(target, opts.left_attr)
                else:
                    cursor = getattr(target, opts.right_attr) + 1
            else:
                level = getattr(target, opts.level_attr) + 1
                if position == "first-child":
                    cursor = getattr(target, opts.left_attr) + 1
                else:
                    cursor = getattr(target, opts.right_attr)
        else:
            tree_id = self._get_next_tree_id()
            cursor = 1
            level = 0

        stack = []

        def treeify(data, cursor=1, level=0):
            data = dict(data)
            children = data.pop("children", [])
            node = self.model(**data)
            stack.append(node)
            setattr(node, opts.tree_id_attr, tree_id)
            setattr(node, opts.level_attr, level)
            setattr(node, opts.left_attr, cursor)
            for child in children:
                cursor = treeify(child, cursor=cursor + 1, level=level + 1)
            cursor += 1
            setattr(node, opts.right_attr, cursor)
            return cursor

        treeify(data, cursor=cursor, level=level)

        if target:
            self._create_space(2 * len(stack), cursor - 1, tree_id)

        return stack

    def _post_insert_update_cached_parent_right(self, instance, right_shift, seen=None):
        setattr(
            instance, self.right_attr, getattr(instance, self.right_attr) + right_shift
        )
        parent = cached_field_value(instance, self.parent_attr)
        if parent:
            if not seen:
                seen = set()
            seen.add(instance)
            if parent in seen:
                # detect infinite recursion and throw an error
                raise InvalidMove
            self._post_insert_update_cached_parent_right(parent, right_shift, seen=seen)

    def _calculate_inter_tree_move_values(self, node, target, position):
        """
        Calculates values required when moving ``node`` relative to
        ``target`` as specified by ``position``.
        """
        left = getattr(node, self.left_attr)
        level = getattr(node, self.level_attr)
        target_left = getattr(target, self.left_attr)
        target_right = getattr(target, self.right_attr)
        target_level = getattr(target, self.level_attr)

        if position == "last-child" or position == "first-child":
            space_target = target_right - 1 if position == "last-child" else target_left
            level_change = level - target_level - 1
            parent = target
        elif position == "left" or position == "right":
            space_target = target_left - 1 if position == "left" else target_right
            level_change = level - target_level
            parent = getattr(target, self.parent_attr)
        else:
            raise ValueError(_("An invalid position was given: %s.") % position)

        left_right_change = left - space_target - 1

        right_shift = 0
        if parent:
            right_shift = 2 * (node.get_descendant_count() + 1)

        return space_target, level_change, left_right_change, parent, right_shift

    def _close_gap(self, size, target, tree_id):
        """
        Closes a gap of a certain ``size`` after the given ``target``
        point in the tree identified by ``tree_id``.
        """
        self._manage_space(-size, target, tree_id)

    def _create_space(self, size, target, tree_id):
        """
        Creates a space of a certain ``size`` after the given ``target``
        point in the tree identified by ``tree_id``.
        """
        self._manage_space(size, target, tree_id)

    def _create_tree_space(self, target_tree_id, num_trees=1):
        """
        Creates space for a new tree by incrementing all tree ids
        greater than ``target_tree_id``.
        """
        qs = self._mptt_filter(tree_id__gt=target_tree_id)
        self._mptt_update(qs, tree_id=F(self.tree_id_attr) + num_trees)
        self.tree_model._mptt_track_tree_insertions(target_tree_id + 1, num_trees)

    def _get_next_tree_id(self):
        """
        Determines the next largest unused tree id for the tree managed by this manager, unless root_node_ordering is
        disabled.  If root_node_ordering is disabled a new default value will be generated for the field (by default
        this will be a new UUID).
        """
        if not self.model._mptt_meta.root_node_ordering:
            return self.model._meta.get_field(self.tree_id_attr).default()

        max_tree_id = next(iter(self.aggregate(Max(self.tree_id_attr)).values()))
        max_tree_id = max_tree_id or 0
        return max_tree_id + 1

    def _inter_tree_move_and_close_gap(
        self, node, level_change, left_right_change, new_tree_id
    ):
        """
        Removes ``node`` from its current tree, with the given set of
        changes being applied to ``node`` and its descendants, closing
        the gap left by moving ``node`` as it does so.
        """
        connection = self._get_connection(instance=node)
        qn = connection.ops.quote_name

        opts = self.model._meta
        root_ordering = self.model._mptt_meta.root_node_ordering
        inter_tree_move_query = """
        UPDATE {table}
        SET {level} = CASE
                WHEN {left} >= %s AND {left} <= %s
                    THEN {level} - %s
                ELSE {level} END,
            {tree_id} = CASE
                WHEN {left} >= %s AND {left} <= %s
                    THEN %s
                ELSE {tree_id} END,
            {left} = CASE
                WHEN {left} >= %s AND {left} <= %s
                    THEN {left} - %s
                WHEN {left} > %s
                    THEN {left} - %s
                ELSE {left} END,
            {right} = CASE
                WHEN {right} >= %s AND {right} <= %s
                    THEN {right} - %s
                WHEN {right} > %s
                    THEN {right} - %s
                ELSE {right} END
        WHERE {tree_id} = %s""".format(
            table=qn(self.tree_model._meta.db_table),
            level=qn(opts.get_field(self.level_attr).column),
            left=qn(opts.get_field(self.left_attr).column),
            tree_id=qn(opts.get_field(self.tree_id_attr).column),
            right=qn(opts.get_field(self.right_attr).column),
        )

        left = getattr(node, self.left_attr)
        right = getattr(node, self.right_attr)
        gap_size = right - left + 1
        gap_target_left = left - 1
        new_tree_id, current_tree_id = clean_tree_ids(
            new_tree_id,
            getattr(node, self.tree_id_attr),
            root_ordering=root_ordering,
            vendor=connection.vendor
        )
        params = [
            left,
            right,
            level_change,
            left,
            right,
            new_tree_id,
            left,
            right,
            left_right_change,
            gap_target_left,
            gap_size,
            left,
            right,
            left_right_change,
            gap_target_left,
            gap_size,
            current_tree_id
        ]

        cursor = connection.cursor()
        cursor.execute(inter_tree_move_query, params)

    def _make_child_root_node(self, node, new_tree_id=None):
        """
        Removes ``node`` from its tree, making it the root node of a new
        tree.

        If ``new_tree_id`` is not specified a new tree id will be
        generated.

        ``node`` will be modified to reflect its new tree state in the
        database.
        """
        left = getattr(node, self.left_attr)
        right = getattr(node, self.right_attr)
        level = getattr(node, self.level_attr)
        if not new_tree_id:
            new_tree_id = self._get_next_tree_id()
        left_right_change = left - 1

        self._inter_tree_move_and_close_gap(node, level, left_right_change, new_tree_id)

        # Update the node to be consistent with the updated
        # tree in the database.
        setattr(node, self.left_attr, left - left_right_change)
        setattr(node, self.right_attr, right - left_right_change)
        setattr(node, self.level_attr, 0)
        setattr(node, self.tree_id_attr, new_tree_id)
        setattr(node, self.parent_attr, None)
        node._mptt_cached_fields[self.parent_attr] = None

    def _make_sibling_of_root_node(self, node, target, position):
        """
        Moves ``node``, making it a sibling of the given ``target`` root
        node as specified by ``position``.

        ``node`` will be modified to reflect its new tree state in the
        database.

        Since we use tree ids to reduce the number of rows affected by
        tree management during insertion and deletion, root nodes are not
        true siblings; thus, making an item a sibling of a root node is
        a special case which involves shuffling tree ids around.
        """
        if node == target:
            raise InvalidMove(_("A node may not be made a sibling of itself."))

        opts = self.model._meta
        root_ordering = self.model._mptt_meta.root_node_ordering
        tree_id = getattr(node, self.tree_id_attr)
        target_tree_id = getattr(target, self.tree_id_attr)

        if node.is_child_node():
            if position == "left":
                space_target = target_tree_id - 1
                new_tree_id = target_tree_id
            elif position == "right":
                space_target = target_tree_id
                new_tree_id = target_tree_id + 1
            else:
                raise ValueError(_("An invalid position was given: %s.") % position)

            self._create_tree_space(space_target)
            if tree_id > space_target:
                # The node's tree id has been incremented in the
                # database - this change must be reflected in the node
                # object for the method call below to operate on the
                # correct tree.
                setattr(node, self.tree_id_attr, tree_id + 1)
            self._make_child_root_node(node, new_tree_id)
        else:
            if position == "left":
                if target_tree_id > tree_id:
                    left_sibling = target.get_previous_sibling()
                    if node == left_sibling:
                        return
                    new_tree_id = getattr(left_sibling, self.tree_id_attr)
                    lower_bound, upper_bound = tree_id, new_tree_id
                    shift = -1
                else:
                    new_tree_id = target_tree_id
                    lower_bound, upper_bound = new_tree_id, tree_id
                    shift = 1
            elif position == "right":
                if target_tree_id > tree_id:
                    new_tree_id = target_tree_id
                    lower_bound, upper_bound = tree_id, target_tree_id
                    shift = -1
                else:
                    right_sibling = target.get_next_sibling()
                    if node == right_sibling:
                        return
                    new_tree_id = getattr(right_sibling, self.tree_id_attr)
                    lower_bound, upper_bound = new_tree_id, tree_id
                    shift = 1
            else:
                raise ValueError(_("An invalid position was given: %s.") % position)

            connection = self._get_connection(instance=node)
            qn = connection.ops.quote_name

            root_sibling_query = """
            UPDATE {table}
            SET {tree_id} = CASE
                WHEN {tree_id} = %s
                    THEN %s
                ELSE {tree_id} + %s END
            WHERE {tree_id} >= %s AND {tree_id} <= %s""".format(
                table=qn(self.tree_model._meta.db_table),
                tree_id=qn(opts.get_field(self.tree_id_attr).column),
            )

            cleaned_tree_id, cleaned_new_tree_id = clean_tree_ids(
                tree_id,
                new_tree_id,
                root_ordering=root_ordering,
                vendor=connection.vendor
            )

            cursor = connection.cursor()
            cursor.execute(
                root_sibling_query,
                [cleaned_tree_id, cleaned_new_tree_id, shift, lower_bound, upper_bound],
            )
            setattr(node, self.tree_id_attr, new_tree_id)

    def _manage_space(self, size, target, tree_id):
        """
        Manages spaces in the tree identified by ``tree_id`` by changing
        the values of the left and right columns by ``size`` after the
        given ``target`` point.
        """
        if self.tree_model._mptt_is_tracking:
            self.tree_model._mptt_track_tree_modified(tree_id)
        else:
            connection = self._get_connection()
            qn = connection.ops.quote_name

            opts = self.model._meta
            root_ordering = self.model._mptt_meta.root_node_ordering
            space_query = """
            UPDATE {table}
            SET {left} = CASE
                    WHEN {left} > %s
                        THEN {left} + %s
                    ELSE {left} END,
                {right} = CASE
                    WHEN {right} > %s
                        THEN {right} + %s
                    ELSE {right} END
            WHERE {tree_id} = %s
              AND ({left} > %s OR {right} > %s)""".format(
                table=qn(self.tree_model._meta.db_table),
                left=qn(opts.get_field(self.left_attr).column),
                right=qn(opts.get_field(self.right_attr).column),
                tree_id=qn(opts.get_field(self.tree_id_attr).column),
            )
            cursor = connection.cursor()
            cursor.execute(
                space_query,
                [
                    target,
                    size,
                    target,
                    size,
                    clean_tree_ids(
                        tree_id,
                        root_ordering=root_ordering,
                        vendor=connection.vendor
                    ),
                    target,
                    target,
                ]
            )

    def _move_child_node(self, node, target, position):
        """
        Calls the appropriate method to move child node ``node``
        relative to the given ``target`` node as specified by
        ``position``.
        """
        tree_id = getattr(node, self.tree_id_attr)
        target_tree_id = getattr(target, self.tree_id_attr)

        if tree_id == target_tree_id:
            self._move_child_within_tree(node, target, position)
        else:
            self._move_child_to_new_tree(node, target, position)

    def _move_child_to_new_tree(self, node, target, position):
        """
        Moves child node ``node`` to a different tree, inserting it
        relative to the given ``target`` node in the new tree as
        specified by ``position``.

        ``node`` will be modified to reflect its new tree state in the
        database.
        """
        left = getattr(node, self.left_attr)
        right = getattr(node, self.right_attr)
        level = getattr(node, self.level_attr)
        new_tree_id = getattr(target, self.tree_id_attr)

        (
            space_target,
            level_change,
            left_right_change,
            parent,
            new_parent_right,
        ) = self._calculate_inter_tree_move_values(node, target, position)

        tree_width = right - left + 1

        # Make space for the subtree which will be moved
        self._create_space(tree_width, space_target, new_tree_id)
        # Move the subtree
        self._inter_tree_move_and_close_gap(
            node, level_change, left_right_change, new_tree_id
        )

        # Update the node to be consistent with the updated
        # tree in the database.
        setattr(node, self.left_attr, left - left_right_change)
        setattr(node, self.right_attr, right - left_right_change)
        setattr(node, self.level_attr, level - level_change)
        setattr(node, self.tree_id_attr, new_tree_id)
        setattr(node, self.parent_attr, parent)

        node._mptt_cached_fields[self.parent_attr] = parent.pk

    def _move_child_within_tree(self, node, target, position):
        """
        Moves child node ``node`` within its current tree relative to
        the given ``target`` node as specified by ``position``.

        ``node`` will be modified to reflect its new tree state in the
        database.
        """
        left = getattr(node, self.left_attr)
        right = getattr(node, self.right_attr)
        level = getattr(node, self.level_attr)
        width = right - left + 1
        tree_id = getattr(node, self.tree_id_attr)
        target_left = getattr(target, self.left_attr)
        target_right = getattr(target, self.right_attr)
        target_level = getattr(target, self.level_attr)

        if position == "last-child" or position == "first-child":
            if node == target:
                raise InvalidMove(_("A node may not be made a child of itself."))
            elif left < target_left < right:
                raise InvalidMove(
                    _("A node may not be made a child of any of its descendants.")
                )
            if position == "last-child":
                if target_right > right:
                    new_left = target_right - width
                    new_right = target_right - 1
                else:
                    new_left = target_right
                    new_right = target_right + width - 1
            else:
                if target_left > left:
                    new_left = target_left - width + 1
                    new_right = target_left
                else:
                    new_left = target_left + 1
                    new_right = target_left + width
            level_change = level - target_level - 1
            parent = target
        elif position == "left" or position == "right":
            if node == target:
                raise InvalidMove(_("A node may not be made a sibling of itself."))
            elif left < target_left < right:
                raise InvalidMove(
                    _("A node may not be made a sibling of any of its descendants.")
                )
            if position == "left":
                if target_left > left:
                    new_left = target_left - width
                    new_right = target_left - 1
                else:
                    new_left = target_left
                    new_right = target_left + width - 1
            else:
                if target_right > right:
                    new_left = target_right - width + 1
                    new_right = target_right
                else:
                    new_left = target_right + 1
                    new_right = target_right + width
            level_change = level - target_level
            parent = getattr(target, self.parent_attr)
        else:
            raise ValueError(_("An invalid position was given: %s.") % position)

        left_boundary = min(left, new_left)
        right_boundary = max(right, new_right)
        left_right_change = new_left - left
        gap_size = width
        if left_right_change > 0:
            gap_size = -gap_size

        connection = self._get_connection(instance=node)
        qn = connection.ops.quote_name

        opts = self.model._meta
        root_ordering = self.model._mptt_meta.root_node_ordering
        # The level update must come before the left update to keep
        # MySQL happy - left seems to refer to the updated value
        # immediately after its update has been specified in the query
        # with MySQL, but not with SQLite or Postgres.
        move_subtree_query = """
        UPDATE {table}
        SET {level} = CASE
                WHEN {left} >= %s AND {left} <= %s
                  THEN {level} - %s
                ELSE {level} END,
            {left} = CASE
                WHEN {left} >= %s AND {left} <= %s
                  THEN {left} + %s
                WHEN {left} >= %s AND {left} <= %s
                  THEN {left} + %s
                ELSE {left} END,
            {right} = CASE
                WHEN {right} >= %s AND {right} <= %s
                  THEN {right} + %s
                WHEN {right} >= %s AND {right} <= %s
                  THEN {right} + %s
                ELSE {right} END
        WHERE {tree_id} = %s""".format(
            table=qn(self.tree_model._meta.db_table),
            level=qn(opts.get_field(self.level_attr).column),
            left=qn(opts.get_field(self.left_attr).column),
            right=qn(opts.get_field(self.right_attr).column),
            tree_id=qn(opts.get_field(self.tree_id_attr).column),
        )

        cursor = connection.cursor()
        cursor.execute(
            move_subtree_query,
            [
                left,
                right,
                level_change,
                left,
                right,
                left_right_change,
                left_boundary,
                right_boundary,
                gap_size,
                left,
                right,
                left_right_change,
                left_boundary,
                right_boundary,
                gap_size,
                clean_tree_ids(
                    tree_id,
                    root_ordering=root_ordering,
                    vendor=connection.vendor
                ),
            ],
        )

        # Update the node to be consistent with the updated
        # tree in the database.
        setattr(node, self.left_attr, new_left)
        setattr(node, self.right_attr, new_right)
        setattr(node, self.level_attr, level - level_change)
        setattr(node, self.parent_attr, parent)
        node._mptt_cached_fields[self.parent_attr] = parent.pk

    def _move_root_node(self, node, target, position):
        """
        Moves root node``node`` to a different tree, inserting it
        relative to the given ``target`` node as specified by
        ``position``.

        ``node`` will be modified to reflect its new tree state in the
        database.
        """
        left = getattr(node, self.left_attr)
        right = getattr(node, self.right_attr)
        level = getattr(node, self.level_attr)
        tree_id = getattr(node, self.tree_id_attr)
        new_tree_id = getattr(target, self.tree_id_attr)
        width = right - left + 1

        if node == target:
            raise InvalidMove(_("A node may not be made a child of itself."))
        elif tree_id == new_tree_id:
            raise InvalidMove(
                _("A node may not be made a child of any of its descendants.")
            )

        (
            space_target,
            level_change,
            left_right_change,
            parent,
            right_shift,
        ) = self._calculate_inter_tree_move_values(node, target, position)

        # Create space for the tree which will be inserted
        self._create_space(width, space_target, new_tree_id)

        # Move the root node, making it a child node
        connection = self._get_connection(instance=node)
        qn = connection.ops.quote_name

        opts = self.model._meta
        root_ordering = self.model._mptt_meta.root_node_ordering
        move_tree_query = """
        UPDATE {table}
        SET {level} = {level} - %s,
            {left} = {left} - %s,
            {right} = {right} - %s,
            {tree_id} = %s
        WHERE {left} >= %s AND {left} <= %s
          AND {tree_id} = %s""".format(
            table=qn(self.tree_model._meta.db_table),
            level=qn(opts.get_field(self.level_attr).column),
            left=qn(opts.get_field(self.left_attr).column),
            right=qn(opts.get_field(self.right_attr).column),
            tree_id=qn(opts.get_field(self.tree_id_attr).column),
        )

        cleaned_tree_id, cleaned_new_tree_id = clean_tree_ids(
            tree_id,
            new_tree_id,
            root_ordering=root_ordering,
            vendor=connection.vendor,
        )

        cursor = connection.cursor()
        cursor.execute(
            move_tree_query,
            [
                level_change,
                left_right_change,
                left_right_change,
                cleaned_new_tree_id,
                left,
                right,
                cleaned_tree_id,
            ],
        )

        # Update the former root node to be consistent with the updated
        # tree in the database.
        setattr(node, self.left_attr, left - left_right_change)
        setattr(node, self.right_attr, right - left_right_change)
        setattr(node, self.level_attr, level - level_change)
        setattr(node, self.tree_id_attr, new_tree_id)
        setattr(node, self.parent_attr, parent)
        node._mptt_cached_fields[self.parent_attr] = parent.pk
