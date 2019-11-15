import difflib
import os
import io
import shutil
import struct
import sys
import torch
import tarfile
import tempfile
import warnings
import copyreg
from contextlib import closing, contextmanager
from ._utils import _import_dotted_name
from ._six import string_classes as _string_classes
from torch._utils_internal import get_source_lines_and_file
if sys.version_info[0] == 2:
    import cPickle as pickle
else:
    import pickle
    import pathlib

DEFAULT_PROTOCOL = 2

LONG_SIZE = struct.Struct('=l').size
INT_SIZE = struct.Struct('=i').size
SHORT_SIZE = struct.Struct('=h').size

MAGIC_NUMBER = 0x1950a86a20f9469cfc6c
PROTOCOL_VERSION = 1001
ZIPFILE_PROTOCOL_VERSION = 1002
STORAGE_KEY_SEPARATOR = ','


class SourceChangeWarning(Warning):
    pass


@contextmanager
def mkdtemp():
    path = tempfile.mkdtemp()
    yield path
    shutil.rmtree(path)


_package_registry = []


def _is_zipfile(f):
    # This is a stricter implementation than zipfile.is_zipfile().
    # zipfile.is_zipfile() is True if the magic number appears anywhere in the
    # binary. Since we expect the files here to be generated by torch.save or
    # torch.jit.save, it's safe to only check the start bytes and avoid
    # collisions. See bugs.python.org/issue28494.

    # Read the first 4 bytes of the file
    read_bytes = []
    start = f.tell()

    f.seek(0)
    byte = f.read(1)
    while byte != "":
        read_bytes.append(byte)
        if len(read_bytes) == 4:
            break
        byte = f.read(1)
    f.seek(start)

    # zip magic numbers
    magic_numbers = [
        ['P', 'K', '\x03', '\x04'],
        ['P', 'K', '\x05', '\x06'],
        ['P', 'K', '\x07', '\x08'],
    ]
    for magic_number in magic_numbers:
        match = True
        for magic_byte, read_byte in zip(magic_number, read_bytes):
            if ord(magic_byte) != ord(read_byte):
                match = False
                break
        if match:
            return True
    return False


def register_package(priority, tagger, deserializer):
    queue_elem = (priority, tagger, deserializer)
    _package_registry.append(queue_elem)
    _package_registry.sort()


def _cpu_tag(obj):
    if type(obj).__module__ == 'torch':
        return 'cpu'


def _cuda_tag(obj):
    if type(obj).__module__ == 'torch.cuda':
        return 'cuda:' + str(obj.get_device())


def _cpu_deserialize(obj, location):
    if location == 'cpu':
        return obj


def validate_cuda_device(location):
    if isinstance(location, torch.device):
        location = str(location)
    if not isinstance(location, _string_classes):
        raise ValueError("location should be a string or torch.device")
    if location[5:] == '':
        device = 0
    else:
        device = max(int(location[5:]), 0)

    if not torch.cuda.is_available():
        raise RuntimeError('Attempting to deserialize object on a CUDA '
                           'device but torch.cuda.is_available() is False. '
                           'If you are running on a CPU-only machine, '
                           'please use torch.load with map_location=torch.device(\'cpu\') '
                           'to map your storages to the CPU.')
    if device >= torch.cuda.device_count():
        raise RuntimeError('Attempting to deserialize object on CUDA device '
                           '{} but torch.cuda.device_count() is {}. Please use '
                           'torch.load with map_location to map your storages '
                           'to an existing device.'.format(
                               device, torch.cuda.device_count()))
    return device


def _cuda_deserialize(obj, location):
    if location.startswith('cuda'):
        device = validate_cuda_device(location)
        if getattr(obj, "_torch_load_uninitialized", False):
            storage_type = getattr(torch.cuda, type(obj).__name__)
            with torch.cuda.device(device):
                return storage_type(obj.size())
        else:
            return obj.cuda(device)


register_package(10, _cpu_tag, _cpu_deserialize)
register_package(20, _cuda_tag, _cuda_deserialize)


def location_tag(storage):
    for _, tagger, _ in _package_registry:
        location = tagger(storage)
        if location:
            return location
    raise RuntimeError("don't know how to determine data location of " +
                       torch.typename(storage))


def default_restore_location(storage, location):
    for _, _, fn in _package_registry:
        result = fn(storage, location)
        if result is not None:
            return result
    raise RuntimeError("don't know how to restore data location of " +
                       torch.typename(storage) + " (tagged with " +
                       location + ")")


def normalize_storage_type(storage_type):
    return getattr(torch, storage_type.__name__)


def storage_to_tensor_type(storage):
    storage_type = type(storage)
    module = _import_dotted_name(storage_type.__module__)
    return getattr(module, storage_type.__name__.replace('Storage', 'Tensor'))


def _is_path(name_or_buffer):
    return isinstance(name_or_buffer, str) or \
        (sys.version_info[0] == 2 and isinstance(name_or_buffer, unicode)) or \
        (sys.version_info[0] == 3 and isinstance(name_or_buffer, pathlib.Path))


class _opener(object):
    def __init__(self, file_like):
        self.file_like = file_like

    def __enter__(self):
        return self.file_like

    def __exit__(self, *args):
        pass


class _open_file(_opener):
    def __init__(self, name, mode):
        super(_open_file, self).__init__(open(name, mode))

    def __exit__(self, *args):
        self.file_like.close()


class _open_buffer_reader(_opener):
    pass


class _open_buffer_writer(_opener):
    def __exit__(self, *args):
        self.file_like.flush()


def _open_file_like(name_or_buffer, mode):
    if _is_path(name_or_buffer):
        return _open_file(name_or_buffer, mode)
    else:
        if 'w' in mode:
            return _open_buffer_writer(name_or_buffer)
        elif 'r' in mode:
            return _open_buffer_reader(name_or_buffer)
        else:
            raise RuntimeError("Expected 'r' or 'w' in mode but got {}".format(mode))


class _open_zipfile_reader(_opener):
    def __init__(self, name_or_buffer):
        super(_open_zipfile_reader, self).__init__(torch._C.PyTorchFileReader(name_or_buffer))


class _open_zipfile_writer_file(_opener):
    def __init__(self, name):
        super(_open_zipfile_writer_file, self).__init__(torch._C.PyTorchFileWriter(name))

    def __exit__(self, *args):
        self.file_like.write_end_of_file()


class _open_zipfile_writer_buffer(_opener):
    def __init__(self, buffer):
        self.buffer = buffer
        super(_open_zipfile_writer_buffer, self).__init__(torch._C.PyTorchFileWriter(buffer))

    def __exit__(self, *args):
        self.file_like.write_end_of_file()
        self.buffer.flush()


def _open_zipfile_writer(name_or_buffer):
    if _is_path(name_or_buffer):
        container = _open_zipfile_writer_file
    else:
        container = _open_zipfile_writer_buffer
    return container(name_or_buffer)


def _is_compressed_file(f):
    compress_modules = ['gzip']
    try:
        return f.__module__ in compress_modules
    except AttributeError:
        return False


def _should_read_directly(f):
    """
    Checks if f is a file that should be read directly. It should be read
    directly if it is backed by a real file (has a fileno) and is not a
    a compressed file (e.g. gzip)
    """
    if _is_compressed_file(f):
        return False
    try:
        return f.fileno() >= 0
    except io.UnsupportedOperation:
        return False
    except AttributeError:
        return False


def _check_seekable(f):

    def raise_err_msg(patterns, e):
        for p in patterns:
            if p in str(e):
                msg = (str(e) + ". You can only torch.load from a file that is seekable." +
                                " Please pre-load the data into a buffer like io.BytesIO and" +
                                " try to load from it instead.")
                raise type(e)(msg)
        raise e

    try:
        f.seek(f.tell())
        return True
    except (io.UnsupportedOperation, AttributeError) as e:
        raise_err_msg(["seek", "tell"], e)


def save(obj, f, pickle_module=pickle, pickle_protocol=DEFAULT_PROTOCOL, _use_new_zipfile_serialization=False):
    """Saves an object to a disk file.

    See also: :ref:`recommend-saving-models`

    Args:
        obj: saved object
        f: a file-like object (has to implement write and flush) or a string
           containing a file name
        pickle_module: module used for pickling metadata and objects
        pickle_protocol: can be specified to override the default protocol

    .. warning::
        If you are using Python 2, :func:`torch.save` does NOT support :class:`StringIO.StringIO`
        as a valid file-like object. This is because the write method should return
        the number of bytes written; :meth:`StringIO.write()` does not do this.

        Please use something like :class:`io.BytesIO` instead.

    Example:
        >>> # Save to file
        >>> x = torch.tensor([0, 1, 2, 3, 4])
        >>> torch.save(x, 'tensor.pt')
        >>> # Save to io.BytesIO buffer
        >>> buffer = io.BytesIO()
        >>> torch.save(x, buffer)
    """
    if _use_new_zipfile_serialization:
        with _open_zipfile_writer(f) as opened_file:
            _save(obj, opened_file, pickle_module, pickle_protocol)
            return

    with _open_file_like(f, 'wb') as opened_file:
        _legacy_save(obj, opened_file, pickle_module, pickle_protocol)


def _legacy_save(obj, f, pickle_module, pickle_protocol):
    if sys.version_info[0] == 2:
        import StringIO
        if isinstance(f, StringIO.StringIO):
            msg = ('torch.save received unsupported StringIO.StringIO file object, whose '
                   'write method does not return the number of bytes written. '
                   'Please use something like io.BytesIO for torch.save instead.')
            raise RuntimeError(msg)

    import torch.nn as nn
    serialized_container_types = {}
    serialized_storages = {}

    def persistent_id(obj):
        # FIXME: the docs say that persistent_id should only return a string
        # but torch store returns tuples. This works only in the binary protocol
        # see
        # https://docs.python.org/2/library/pickle.html#pickling-and-unpickling-external-objects
        # https://github.com/python/cpython/blob/master/Lib/pickle.py#L527-L537
        if isinstance(obj, type) and issubclass(obj, nn.Module):
            if obj in serialized_container_types:
                return None
            serialized_container_types[obj] = True
            source_file = source = None
            try:
                source_lines, _, source_file = get_source_lines_and_file(obj)
                source = ''.join(source_lines)
            except Exception:  # saving the source is optional, so we can ignore any errors
                warnings.warn("Couldn't retrieve source code for container of "
                              "type " + obj.__name__ + ". It won't be checked "
                              "for correctness upon loading.")
            return ('module', obj, source_file, source)

        elif torch.is_storage(obj):
            storage_type = normalize_storage_type(type(obj))
            # Offset is always 0, but we keep it for backwards compatibility
            # with the old serialization format (which supported storage views)
            offset = 0
            obj_key = str(obj._cdata)
            location = location_tag(obj)
            serialized_storages[obj_key] = obj
            is_view = obj._cdata != obj._cdata
            if is_view:
                view_metadata = (str(obj._cdata), offset, obj.size())
            else:
                view_metadata = None

            return ('storage',
                    storage_type,
                    obj_key,
                    location,
                    obj.size(),
                    view_metadata)
        return None

    sys_info = dict(
        protocol_version=PROTOCOL_VERSION,
        little_endian=sys.byteorder == 'little',
        type_sizes=dict(
            short=SHORT_SIZE,
            int=INT_SIZE,
            long=LONG_SIZE,
        ),
    )

    pickle_module.dump(MAGIC_NUMBER, f, protocol=pickle_protocol)
    pickle_module.dump(PROTOCOL_VERSION, f, protocol=pickle_protocol)
    pickle_module.dump(sys_info, f, protocol=pickle_protocol)
    pickler = pickle_module.Pickler(f, protocol=pickle_protocol)
    pickler.persistent_id = persistent_id
    pickler.dump(obj)

    serialized_storage_keys = sorted(serialized_storages.keys())
    pickle_module.dump(serialized_storage_keys, f, protocol=pickle_protocol)
    f.flush()
    for key in serialized_storage_keys:
        serialized_storages[key]._write_file(f, _should_read_directly(f))


def _save(obj, zip_file, pickle_module, pickle_protocol):
    serialized_storages = {}

    def persistent_id(obj):
        # FIXME: the docs say that persistent_id should only return a string
        # but torch store returns tuples. This works only in the binary protocol
        # see
        # https://docs.python.org/2/library/pickle.html#pickling-and-unpickling-external-objects
        # https://github.com/python/cpython/blob/master/Lib/pickle.py#L527-L537
        if torch.is_storage(obj):
            storage_type = normalize_storage_type(type(obj))
            obj_key = str(obj._cdata)
            location = location_tag(obj)
            serialized_storages[obj_key] = obj

            return ('storage',
                    storage_type,
                    obj_key,
                    location,
                    obj.size())
        return None

    # Write the pickle data for `obj`
    data_buf = io.BytesIO()
    pickler = pickle_module.Pickler(data_buf, protocol=pickle_protocol)
    pickler.persistent_id = persistent_id
    pickler.dump(obj)
    data_value = data_buf.getvalue()
    zip_file.write_record('data.pkl', data_value, len(data_value))

    # Write each tensor to a file named tensor/the_tensor_key in the zip archive
    for key in sorted(serialized_storages.keys()):
        name = 'tensors/{}'.format(key)
        storage = serialized_storages[key]
        num_bytes = storage.size() * storage.element_size()
        zip_file.write_record(name, storage.data_ptr(), num_bytes)


def load(f, map_location=None, pickle_module=pickle, **pickle_load_args):
    """Loads an object saved with :func:`torch.save` from a file.

    :func:`torch.load` uses Python's unpickling facilities but treats storages,
    which underlie tensors, specially. They are first deserialized on the
    CPU and are then moved to the device they were saved from. If this fails
    (e.g. because the run time system doesn't have certain devices), an exception
    is raised. However, storages can be dynamically remapped to an alternative
    set of devices using the :attr:`map_location` argument.

    If :attr:`map_location` is a callable, it will be called once for each serialized
    storage with two arguments: storage and location. The storage argument
    will be the initial deserialization of the storage, residing on the CPU.
    Each serialized storage has a location tag associated with it which
    identifies the device it was saved from, and this tag is the second
    argument passed to :attr:`map_location`. The builtin location tags are ``'cpu'``
    for CPU tensors and ``'cuda:device_id'`` (e.g. ``'cuda:2'``) for CUDA tensors.
    :attr:`map_location` should return either ``None`` or a storage. If
    :attr:`map_location` returns a storage, it will be used as the final deserialized
    object, already moved to the right device. Otherwise, :func:`torch.load` will
    fall back to the default behavior, as if :attr:`map_location` wasn't specified.

    If :attr:`map_location` is a :class:`torch.device` object or a string contraining
    a device tag, it indicates the location where all tensors should be loaded.

    Otherwise, if :attr:`map_location` is a dict, it will be used to remap location tags
    appearing in the file (keys), to ones that specify where to put the
    storages (values).

    User extensions can register their own location tags and tagging and
    deserialization methods using :func:`torch.serialization.register_package`.

    Args:
        f: a file-like object (has to implement :meth:`read`, :meth`readline`, :meth`tell`, and :meth`seek`),
            or a string containing a file name
        map_location: a function, :class:`torch.device`, string or a dict specifying how to remap storage
            locations
        pickle_module: module used for unpickling metadata and objects (has to
            match the :attr:`pickle_module` used to serialize file)
        pickle_load_args: (Python 3 only) optional keyword arguments passed over to
            :func:`pickle_module.load` and :func:`pickle_module.Unpickler`, e.g.,
            :attr:`errors=...`.

    .. note::
        When you call :func:`torch.load()` on a file which contains GPU tensors, those tensors
        will be loaded to GPU by default. You can call ``torch.load(.., map_location='cpu')``
        and then :meth:`load_state_dict` to avoid GPU RAM surge when loading a model checkpoint.

    .. note::
        By default, we decode byte strings as ``utf-8``.  This is to avoid a common error
        case ``UnicodeDecodeError: 'ascii' codec can't decode byte 0x...``
        when loading files saved by Python 2 in Python 3.  If this default
        is incorrect, you may use an extra :attr:`encoding` keyword argument to specify how
        these objects should be loaded, e.g., :attr:`encoding='latin1'` decodes them
        to strings using ``latin1`` encoding, and :attr:`encoding='bytes'` keeps them
        as byte arrays which can be decoded later with ``byte_array.decode(...)``.

    Example:
        >>> torch.load('tensors.pt')
        # Load all tensors onto the CPU
        >>> torch.load('tensors.pt', map_location=torch.device('cpu'))
        # Load all tensors onto the CPU, using a function
        >>> torch.load('tensors.pt', map_location=lambda storage, loc: storage)
        # Load all tensors onto GPU 1
        >>> torch.load('tensors.pt', map_location=lambda storage, loc: storage.cuda(1))
        # Map tensors from GPU 1 to GPU 0
        >>> torch.load('tensors.pt', map_location={'cuda:1':'cuda:0'})
        # Load tensor from io.BytesIO object
        >>> with open('tensor.pt', 'rb') as f:
                buffer = io.BytesIO(f.read())
        >>> torch.load(buffer)
        # Load a module with 'ascii' encoding for unpickling
        >>> torch.load('module.pt', encoding='ascii')
    """
    if sys.version_info >= (3, 0) and 'encoding' not in pickle_load_args.keys():
        pickle_load_args['encoding'] = 'utf-8'

    with _open_file_like(f, 'rb') as opened_file:
        if _is_zipfile(opened_file):
            with _open_zipfile_reader(f) as opened_zipfile:
                return _load(opened_zipfile, map_location, pickle_module, **pickle_load_args)
        return _legacy_load(opened_file, map_location, pickle_module, **pickle_load_args)


# Register pickling support for layout instances such as
# torch.sparse_coo, etc
def _get_layout(name):
    """Get layout extension object from its string representation.
    """
    cache = _get_layout.cache
    if not cache:
        for v in torch.__dict__.values():
            if isinstance(v, torch.layout):
                cache[str(v)] = v
    return cache[name]


_get_layout.cache = {}
copyreg.pickle(torch.layout, lambda obj: (_get_layout, (str(obj),)))


def _legacy_load(f, map_location, pickle_module, **pickle_load_args):
    deserialized_objects = {}

    restore_location = _get_restore_location(map_location)

    def _check_container_source(container_type, source_file, original_source):
        try:
            current_source = ''.join(get_source_lines_and_file(container_type)[0])
        except Exception:  # saving the source is optional, so we can ignore any errors
            warnings.warn("Couldn't retrieve source code for container of "
                          "type " + container_type.__name__ + ". It won't be checked "
                          "for correctness upon loading.")
            return
        if original_source != current_source:
            if container_type.dump_patches:
                file_name = container_type.__name__ + '.patch'
                diff = difflib.unified_diff(current_source.split('\n'),
                                            original_source.split('\n'),
                                            source_file,
                                            source_file, lineterm="")
                lines = '\n'.join(diff)
                try:
                    with open(file_name, 'a+') as f:
                        file_size = f.seek(0, 2)
                        f.seek(0)
                        if file_size == 0:
                            f.write(lines)
                        elif file_size != len(lines) or f.read() != lines:
                            raise IOError
                    msg = ("Saved a reverse patch to " + file_name + ". "
                           "Run `patch -p0 < " + file_name + "` to revert your "
                           "changes.")
                except IOError:
                    msg = ("Tried to save a patch, but couldn't create a "
                           "writable file " + file_name + ". Make sure it "
                           "doesn't exist and your working directory is "
                           "writable.")
            else:
                msg = ("you can retrieve the original source code by "
                       "accessing the object's source attribute or set "
                       "`torch.nn.Module.dump_patches = True` and use the "
                       "patch tool to revert the changes.")
            msg = ("source code of class '{}' has changed. {}"
                   .format(torch.typename(container_type), msg))
            warnings.warn(msg, SourceChangeWarning)

    def legacy_load(f):
        deserialized_objects = {}

        def persistent_load(saved_id):
            if isinstance(saved_id, tuple):
                # Ignore containers that don't have any sources saved
                if all(saved_id[1:]):
                    _check_container_source(*saved_id)
                return saved_id[0]
            return deserialized_objects[int(saved_id)]

        with closing(tarfile.open(fileobj=f, mode='r:', format=tarfile.PAX_FORMAT)) as tar, \
                mkdtemp() as tmpdir:

            tar.extract('storages', path=tmpdir)
            with open(os.path.join(tmpdir, 'storages'), 'rb', 0) as f:
                num_storages = pickle_module.load(f, **pickle_load_args)
                for i in range(num_storages):
                    args = pickle_module.load(f, **pickle_load_args)
                    key, location, storage_type = args
                    obj = storage_type._new_with_file(f)
                    obj = restore_location(obj, location)
                    deserialized_objects[key] = obj

                storage_views = pickle_module.load(f, **pickle_load_args)
                for target_cdata, root_cdata, offset, size in storage_views:
                    root = deserialized_objects[root_cdata]
                    deserialized_objects[target_cdata] = root[offset:offset + size]

            tar.extract('tensors', path=tmpdir)
            with open(os.path.join(tmpdir, 'tensors'), 'rb', 0) as f:
                num_tensors = pickle_module.load(f, **pickle_load_args)
                for _ in range(num_tensors):
                    args = pickle_module.load(f, **pickle_load_args)
                    key, storage_id, original_tensor_type = args
                    storage = deserialized_objects[storage_id]
                    tensor_type = storage_to_tensor_type(storage)
                    ndim, = struct.unpack('<i', f.read(4))
                    # skip next 4 bytes; legacy encoding treated ndim as 8 bytes
                    f.read(4)
                    size = struct.unpack('<{}q'.format(ndim), f.read(8 * ndim))
                    stride = struct.unpack('<{}q'.format(ndim), f.read(8 * ndim))
                    storage_offset, = struct.unpack('<q', f.read(8))
                    tensor = tensor_type().set_(storage, storage_offset, size, stride)
                    deserialized_objects[key] = tensor

            pickle_file = tar.extractfile('pickle')
            unpickler = pickle_module.Unpickler(pickle_file, **pickle_load_args)
            unpickler.persistent_load = persistent_load
            result = unpickler.load()
            return result

    deserialized_objects = {}

    def persistent_load(saved_id):
        assert isinstance(saved_id, tuple)
        typename = _maybe_decode_ascii(saved_id[0])
        data = saved_id[1:]

        if typename == 'module':
            # Ignore containers that don't have any sources saved
            if all(data[1:]):
                _check_container_source(*data)
            return data[0]
        elif typename == 'storage':
            data_type, root_key, location, size, view_metadata = data
            location = _maybe_decode_ascii(location)
            if root_key not in deserialized_objects:
                obj = data_type(size)
                obj._torch_load_uninitialized = True
                deserialized_objects[root_key] = restore_location(obj, location)
            storage = deserialized_objects[root_key]
            if view_metadata is not None:
                view_key, offset, view_size = view_metadata
                if view_key not in deserialized_objects:
                    deserialized_objects[view_key] = storage[offset:offset + view_size]
                return deserialized_objects[view_key]
            else:
                return storage
        else:
            raise RuntimeError("Unknown saved id type: %s" % saved_id[0])

    _check_seekable(f)
    f_should_read_directly = _should_read_directly(f)

    if f_should_read_directly and f.tell() == 0:
        # legacy_load requires that f has fileno()
        # only if offset is zero we can attempt the legacy tar file loader
        try:
            return legacy_load(f)
        except tarfile.TarError:
            if _is_zipfile(f):
                # .zip is used for torch.jit.save and will throw an un-pickling error here
                raise RuntimeError("{} is a zip archive (did you mean to use torch.jit.load()?)".format(f.name))
            # if not a tarfile, reset file offset and proceed
            f.seek(0)

    magic_number = pickle_module.load(f, **pickle_load_args)
    if magic_number != MAGIC_NUMBER:
        raise RuntimeError("Invalid magic number; corrupt file?")
    protocol_version = pickle_module.load(f, **pickle_load_args)
    if protocol_version != PROTOCOL_VERSION:
        raise RuntimeError("Invalid protocol version: %s" % protocol_version)

    _sys_info = pickle_module.load(f, **pickle_load_args)
    unpickler = pickle_module.Unpickler(f, **pickle_load_args)
    unpickler.persistent_load = persistent_load
    result = unpickler.load()

    deserialized_storage_keys = pickle_module.load(f, **pickle_load_args)

    offset = f.tell() if f_should_read_directly else None
    for key in deserialized_storage_keys:
        assert key in deserialized_objects
        deserialized_objects[key]._set_from_file(f, offset, f_should_read_directly)
        if offset is not None:
            offset = f.tell()

    return result


def _maybe_decode_ascii(bytes_str):
    # When using encoding='bytes' in Py3, some **internal** keys stored as
    # strings in Py2 are loaded as bytes. This function decodes them with
    # ascii encoding, one that Py3 uses by default.
    #
    # NOTE: This should only be used on internal keys (e.g., `typename` and
    #       `location` in `persistent_load` below!
    if isinstance(bytes_str, bytes):
        return bytes_str.decode('ascii')
    return bytes_str


def _get_restore_location(map_location):
    if map_location is None:
        restore_location = default_restore_location
    elif isinstance(map_location, dict):
        def restore_location(storage, location):
            location = map_location.get(location, location)
            return default_restore_location(storage, location)
    elif isinstance(map_location, _string_classes):
        def restore_location(storage, location):
            return default_restore_location(storage, map_location)
    elif isinstance(map_location, torch.device):
        def restore_location(storage, location):
            return default_restore_location(storage, str(map_location))
    else:
        def restore_location(storage, location):
            result = map_location(storage, location)
            if result is None:
                result = default_restore_location(storage, location)
            return result
    return restore_location


def _load(zip_file, map_location, pickle_module, **pickle_load_args):
    restore_location = _get_restore_location(map_location)

    loaded_storages = {}

    def load_tensor(obj, size, key, location):
        loaded_storages[key] = restore_location(obj, location)
        name = 'tensors/{}'.format(key)
        size_long = struct.pack("<Q", size)
        tensor_file = io.BytesIO(size_long + zip_file.get_record(name))
        offset = None
        is_real_file = False
        loaded_storages[key]._set_from_file(tensor_file, offset, is_real_file)

    def persistent_load(saved_id):
        assert isinstance(saved_id, tuple)
        typename = _maybe_decode_ascii(saved_id[0])
        data = saved_id[1:]

        assert typename == 'storage', \
            "Unknown typename for persistent_load, expected 'storage' but got '{}'".format(typename)

        data_type, key, location, size = data
        if key not in loaded_storages:
            load_tensor(data_type(size), size, key, _maybe_decode_ascii(location))
        storage = loaded_storages[key]
        return storage

    # Load the data (which may in turn use `persistent_load` to load tensors)
    data_file = io.BytesIO(zip_file.get_record('data.pkl'))
    unpickler = pickle_module.Unpickler(data_file, **pickle_load_args)
    unpickler.persistent_load = persistent_load
    result = unpickler.load()

    return result
