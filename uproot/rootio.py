#!/usr/bin/env python

# Copyright (c) 2017, DIANA-HEP
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import keyword
import numbers
import re
import struct
import sys
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse

import numpy

import uproot.const
import uproot.source.compressed
from uproot.source.memmap import MemmapSource
from uproot.source.xrootd import XRootDSource
from uproot.source.cursor import Cursor

################################################################ register mixins for user-facing ROOT classes

methods = {}

################################################################ high-level interface

def open(path, localsource=MemmapSource.defaults, xrootdsource=XRootDSource.defaults, **options):
    parsed = urlparse(path)
    if _bytesid(parsed.scheme) == b"file" or len(parsed.scheme) == 0:
        path = parsed.netloc + parsed.path
        return ROOTDirectory.read(localsource(path), **options)

    elif _bytesid(parsed.scheme) == b"root":
        return xrootd(path, xrootdsource)

    else:
        raise ValueError("URI scheme not recognized: {0}".format(path))

def xrootd(path, xrootdsource=XRootDSource.defaults, **options):
    return ROOTDirectory.read(xrootdsource(path), **options)

def nofilter(x): return True

################################################################ ROOTDirectory

class ROOTDirectory(object):
    # makes __doc__ attribute mutable before Python 3.3
    __metaclass__ = type.__new__(type, "type", (type,), {})

    classname = b"TDirectory"

    class _FileContext(object):
        def __init__(self, sourcepath, streamerinfos, streamerinfosmap, classes, compression, tfile):
            self.sourcepath, self.streamerinfos, self.streamerinfosmap, self.classes, self.compression, self.tfile = sourcepath, streamerinfos, streamerinfosmap, classes, compression, tfile

        def copy(self):
            out = ROOTDirectory._FileContext.__new__(ROOTDirectory._FileContext)
            out.__dict__.update(self.__dict__)
            return out

    @staticmethod
    def read(source, *args, **options):
        # make sure that all methods classes have been loaded
        import uproot.tree
        import uproot.functional
        import uproot.hist

        if len(args) == 0:
            try:
                read_streamers = options.pop("read_streamers", True)
                if len(options) > 0:
                    raise TypeError("unrecognized options: {0}".format(", ".join(options)))

                # See https://root.cern/doc/master/classTFile.html
                cursor = Cursor(0)
                magic, fVersion = cursor.fields(source, ROOTDirectory._format1)
                if magic != b"root":
                    raise ValueError("not a ROOT file (starts with {0} instead of 'root')".format(repr(magic)))
                if fVersion < 1000000:
                    fBEGIN, fEND, fSeekFree, fNbytesFree, nfree, fNbytesName, fUnits, fCompress, fSeekInfo, fNbytesInfo, fUUID = cursor.fields(source, ROOTDirectory._format2_small)
                else:
                    fBEGIN, fEND, fSeekFree, fNbytesFree, nfree, fNbytesName, fUnits, fCompress, fSeekInfo, fNbytesInfo, fUUID = cursor.fields(source, ROOTDirectory._format2_big)

                tfile = {"fVersion": fVersion, "fBEGIN": fBEGIN, "fEND": fEND, "fSeekFree": fSeekFree, "fNbytesFree": fNbytesFree, "nfree": nfree, "fNbytesName": fNbytesName, "fUnits": fUnits, "fCompress": fCompress, "fSeekInfo": fSeekInfo, "fNbytesInfo": fNbytesInfo, "fUUID": fUUID}

                # classes requried to read streamers (bootstrap)
                streamerclasses = {"TStreamerInfo":             TStreamerInfo,
                                   "TStreamerElement":          TStreamerElement,
                                   "TStreamerBase":             TStreamerBase,
                                   "TStreamerBasicType":        TStreamerBasicType,
                                   "TStreamerBasicPointer":     TStreamerBasicPointer,
                                   "TStreamerLoop":             TStreamerLoop,
                                   "TStreamerObject":           TStreamerObject,
                                   "TStreamerObjectPointer":    TStreamerObjectPointer,
                                   "TStreamerObjectAny":        TStreamerObjectAny,
                                   "TStreamerObjectAnyPointer": TStreamerObjectAnyPointer,
                                   "TStreamerString":           TStreamerString,
                                   "TStreamerSTL":              TStreamerSTL,
                                   "TStreamerSTLstring":        TStreamerSTLstring,
                                   "TStreamerArtificial":       TStreamerArtificial,
                                   "TList":                     TList,
                                   "TObjArray":                 TObjArray,
                                   "TObjString":                TObjString}

                if read_streamers:
                    streamercontext = ROOTDirectory._FileContext(source.path, None, None, streamerclasses, uproot.source.compressed.Compression(fCompress), tfile)
                    streamerkey = TKey.read(source, Cursor(fSeekInfo), streamercontext)
                    streamerinfos, streamerinfosmap, streamerrules = _readstreamers(streamerkey._source, streamerkey._cursor, streamercontext)
                else:
                    streamerinfos, streamerinfosmap, streamerrules = [], {}, []

                classes = _defineclasses(streamerinfos)
                context = ROOTDirectory._FileContext(source.path, streamerinfos, streamerinfosmap, classes, uproot.source.compressed.Compression(fCompress), tfile)

                keycursor = Cursor(fBEGIN)
                mykey = TKey.read(source, keycursor, context)

                return ROOTDirectory.read(source, Cursor(fBEGIN + fNbytesName), context, mykey)

            except:
                source.dismiss()
                raise

        else:
            try:
                if len(options) > 0:
                    raise TypeError("unrecognized options: {0}".format(", ".join(options)))

                cursor, context, mykey = args

                # See https://root.cern/doc/master/classTDirectoryFile.html.
                fVersion, fDatimeC, fDatimeM, fNbytesKeys, fNbytesName = cursor.fields(source, ROOTDirectory._format3)
                if fVersion <= 1000:
                    fSeekDir, fSeekParent, fSeekKeys = cursor.fields(source, ROOTDirectory._format4_small)
                else:
                    fSeekDir, fSeekParent, fSeekKeys = cursor.fields(source, ROOTDirectory._format4_big)

                subcursor = Cursor(fSeekKeys)
                headerkey = TKey.read(source, subcursor, context)

                nkeys = subcursor.field(source, ROOTDirectory._format5)
                keys = [TKey.read(source, subcursor, context) for i in range(nkeys)]

                out = ROOTDirectory(mykey.fName, context, keys)
                out.fVersion, out.fDatimeC, out.fDatimeM, out.fNbytesKeys, out.fNbytesName, out.fSeekDir, out.fSeekParent, out.fSeekKeys = fVersion, fDatimeC, fDatimeM, fNbytesKeys, fNbytesName, fSeekDir, fSeekParent, fSeekKeys
                out._headerkey = headerkey
                return out

            finally:
                source.dismiss()

    _format1       = struct.Struct(">4si")
    _format2_small = struct.Struct(">iiiiiiBiii18s")
    _format2_big   = struct.Struct(">iqqiiiBiqi18s")
    _format3       = struct.Struct(">hIIii")
    _format4_small = struct.Struct(">iii")
    _format4_big   = struct.Struct(">qqq")
    _format5       = struct.Struct(">i")

    def __init__(self, name, context, keys):
        self.name, self._context, self._keys = name, context, keys

    @property
    def compression(self):
        return self._context.compression

    def __repr__(self):
        return "<ROOTDirectory {0} at 0x{1:012x}>".format(repr(self.name), id(self))

    def __getitem__(self, name):
        return self.get(name)

    def __len__(self):
        return len(self._keys)

    def __iter__(self):
        return self.keys()

    @staticmethod
    def _withoutcycle(key):
        return "{0}".format(key.fName.decode("ascii")).encode("ascii")

    @staticmethod
    def _withcycle(key):
        return "{0};{1}".format(key.fName.decode("ascii"), key.fCycle).encode("ascii")

    def showstreamers(self, filtername=nofilter, stream=sys.stdout):
        if stream is None:
            return "\n".join(x.show(stream=stream) for x in self._context.streamerinfos if filtername(x.fName))
        else:
            for x in self._context.streamerinfos:
                if filtername(x.fName):
                    x.show(stream=stream)

    def _classof(self, classname):
        if classname == b"TDirectory":
            cls = ROOTDirectory
        else:
            cls = self._context.classes.get(classname, None)
            if cls is None:
                cls = ROOTObject.__metaclass__("Undefined_" + str(_safename(classname)), (Undefined,), {"classname": classname})
        return cls

    def iterkeys(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        for key in self._keys:
            cls = self._classof(key.fClassName)
            if filtername(key.fName) and filterclass(cls):
                yield self._withcycle(key)

            if recursive and key.fClassName == b"TDirectory":
                for name in key.get().iterkeys(recursive, filtername, filterclass):
                    yield "{0}/{1}".format(self._withoutcycle(key).decode("ascii"), name.decode("ascii")).encode("ascii")

    def itervalues(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        for key in self._keys:
            cls = self._classof(key.fClassName)
            if filtername(key.fName) and filterclass(cls):
                yield key.get()

            if recursive and key.fClassName == b"TDirectory":
                for value in key.get().itervalues(recursive, filtername, filterclass):
                    yield value

    def iteritems(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        for key in self._keys:
            cls = self._classof(key.fClassName)
            if filtername(key.fName) and filterclass(cls):
                yield self._withcycle(key), key.get()

            if recursive and key.fClassName == b"TDirectory":
                for name, value in key.get().iteritems(recursive, filtername, filterclass):
                    yield "{0}/{1}".format(self._withoutcycle(key).decode("ascii"), name.decode("ascii")).encode("ascii"), value

    def iterclasses(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        for key in self._keys:
            cls = self._classof(key.fClassName)
            if filtername(key.fName) and filterclass(cls):
                yield self._withcycle(key), cls

            if recursive and key.fClassName == b"TDirectory":
                for name, classname in key.get().iterclasses(recursive, filtername, filterclass):
                    yield "{0}/{1}".format(self._withoutcycle(key).decode("ascii"), name.decode("ascii")).encode("ascii"), classname

    def keys(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        return list(self.iterkeys(recursive=recursive, filtername=filtername, filterclass=filterclass))

    def values(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        return list(self.itervalues(recursive=recursive, filtername=filtername, filterclass=filterclass))

    def items(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        return list(self.iteritems(recursive=recursive, filtername=filtername, filterclass=filterclass))

    def classes(self, recursive=False, filtername=nofilter, filterclass=nofilter):
        return list(self.iterclasses(recursive=recursive, filtername=filtername, filterclass=filterclass))

    def allkeys(self, filtername=nofilter, filterclass=nofilter):
        return self.keys(recursive=True, filtername=filtername, filterclass=filterclass)

    def allvalues(self, filtername=nofilter, filterclass=nofilter):
        return self.values(recursive=True, filtername=filtername, filterclass=filterclass)

    def allitems(self, filtername=nofilter, filterclass=nofilter):
        return self.items(recursive=True, filtername=filtername, filterclass=filterclass)

    def allclasses(self, filtername=nofilter, filterclass=nofilter):
        return self.classes(recursive=True, filtername=filtername, filterclass=filterclass)

    def get(self, name, cycle=None):
        name = _bytesid(name)

        if b"/" in name:
            out = self
            for n in name.split(b"/"):
                out = out.get(n, cycle)
            return out

        else:
            if cycle is None and b";" in name:
                at = name.rindex(b";")
                name, cycle = name[:at], name[at + 1:]
                cycle = int(cycle)

            for key in self._keys:
                if key.fName == name:
                    if cycle is None or key.fCycle == cycle:
                        return key.get()
            raise KeyError("not found: {0}".format(repr(name)))

    def __contains__(self, name):
        try:
            self.get(name)
        except KeyError:
            return False
        else:
            return True

    def __enter__(self, *args, **kwds):
        return self

    def __exit__(self, *args, **kwds):
        pass

################################################################ helper functions for common tasks

def _bytesid(x):
    if sys.version_info[0] > 2:
        if isinstance(x, str):
            return x.encode("ascii", "backslashreplace")
        else:
            return x
    else:
        if isinstance(x, unicode):
            return x.encode("ascii", "backslashreplace")
        else:
            return x

def _startcheck(source, cursor):
    start = cursor.index
    cnt, vers = cursor.fields(source, _startcheck._format_cntvers)
    cnt = int(numpy.int64(cnt) & ~uproot.const.kByteCountMask)
    return start, cnt + 4, vers
_startcheck._format_cntvers = struct.Struct(">IH")

def _endcheck(start, cursor, cnt):
    observed = cursor.index - start
    if observed != cnt:
        raise ValueError("object has {0} bytes; expected {1}".format(observed, cnt))

def _skiptobj(source, cursor):
    version = cursor.field(source, _skiptobj._format1)
    if numpy.int64(version) & uproot.const.kByteCountVMask:
        cursor.skip(4)
    fUniqueID, fBits = cursor.fields(source, _skiptobj._format2)
    fBits = numpy.uint32(fBits) | uproot.const.kIsOnHeap
    if fBits & uproot.const.kIsReferenced:
        cursor.skip(2)
_skiptobj._format1 = struct.Struct(">h")
_skiptobj._format2 = struct.Struct(">II")

def _nametitle(source, cursor):
    start, cnt, vers = _startcheck(source, cursor)
    _skiptobj(source, cursor)
    name = cursor.string(source)
    title = cursor.string(source)
    _endcheck(start, cursor, cnt)
    return name, title

def _readobjany(source, cursor, context, asclass=None):
    # TBufferFile::ReadObjectAny()
    # https://github.com/root-project/root/blob/c4aa801d24d0b1eeb6c1623fd18160ef2397ee54/io/io/src/TBufferFile.cxx#L2404

    beg = cursor.index - cursor.origin
    bcnt = cursor.field(source, struct.Struct(">I"))

    if numpy.int64(bcnt) & uproot.const.kByteCountMask == 0 or numpy.int64(bcnt) == uproot.const.kNewClassTag:
        vers = 0
        start = 0
        tag = bcnt
        bcnt = 0
    else:
        vers = 1
        start = cursor.index - cursor.origin
        tag = cursor.field(source, struct.Struct(">I"))

    if numpy.int64(tag) & uproot.const.kClassMask == 0:
        # reference object
        if tag == 0:
            return None                                    # return null

        elif tag == 1:
            raise NotImplementedError("tag == 1 means self; not implemented yet")

        elif tag not in cursor.refs:
            # jump past this object
            cursor.index = cursor.origin + beg + bcnt + 4
            return None                                    # return null

        else:
            return cursor.refs[tag]                        # return object

    elif tag == uproot.const.kNewClassTag:
        # new class and object
        cname = cursor.cstring(source).decode("ascii")
            
        fct = context.classes.get(cname, Undefined)

        if vers > 0:
            cursor.refs[start + uproot.const.kMapOffset] = fct
        else:
            cursor.refs[len(cursor.refs) + 1] = fct
        
        if asclass is None:
            obj = fct.read(source, cursor, context)        # new object
            if isinstance(obj, Undefined):
                obj.classname = cname
        else:
            obj = asclass.read(source, cursor, context)    # placeholder new object

        if vers > 0:
            cursor.refs[beg + uproot.const.kMapOffset] = obj
        else:
            cursor.refs[len(cursor.refs) + 1] = obj

        return obj                                         # return object

    else:
        # reference class, new object
        ref = int(numpy.int64(tag) & ~uproot.const.kClassMask)

        if asclass is None:
            if ref not in cursor.refs:
                raise IOError("invalid class-tag reference")

            fct = cursor.refs[ref]                         # reference class

            if fct not in context.classes.values():
                raise IOError("invalid class-tag reference (not a recognized class: {0})".format(fct))

            obj = fct.read(source, cursor, context)        # new object

        else:
            obj = asclass.read(source, cursor, context)    # placeholder new object

        if vers > 0:
            cursor.refs[beg + uproot.const.kMapOffset] = obj
        else:
            cursor.refs[len(cursor.refs) + 1] = obj

        return obj                                         # return object

def _readstreamers(source, cursor, context):
    tlist = TList.read(source, cursor, context)

    streamerinfos = []
    streamerrules = []
    for obj in tlist:
        if isinstance(obj, TStreamerInfo):
            dependencies = set()
            for element in obj.fElements:
                if isinstance(element, TStreamerBase):
                    dependencies.add(element.fName)
                if isinstance(element, (TStreamerObject, TStreamerObjectAny, TStreamerString)) or (isinstance(element, TStreamerObjectPointer) and element.fType == uproot.const.kObjectp):
                    dependencies.add(element.fTypeName.rstrip(b"*"))
            streamerinfos.append((obj, dependencies))

        elif isinstance(obj, TList) and all(isinstance(x, TObjString) for x in obj):
            streamerrules.append(obj)

        else:
            raise ValueError("expected TStreamerInfo or TList of TObjString in streamer info array")

    # https://stackoverflow.com/a/11564769/1623645
    def topological_sort(items):
        provided = set([x.encode("ascii") for x in builtin_classes])
        while len(items) > 0:
            remaining_items = []
            emitted = False

            for item, dependencies in items:
                if dependencies.issubset(provided):
                    yield item
                    provided.add(item.fName)
                    emitted = True
                else:
                    remaining_items.append((item, dependencies))

            if not emitted:
                raise ValueError("cannot sort TStreamerInfos into dependency order:\n\n{0}".format("\n".join("{0:20s} requires {1}".format(item.fName, " ".join(dependencies)) for item, dependencies in items)))

            items = remaining_items

    streamerinfos = list(topological_sort(streamerinfos))
    streamerinfosmap = dict((x.fName, x) for x in streamerinfos)

    for streamerinfo in streamerinfos:
        streamerinfo.members = {}
        for element in streamerinfo.fElements:
            if isinstance(element, TStreamerBase):
                if element.fName in streamerinfosmap:
                    streamerinfo.members.update(getattr(streamerinfosmap[element.fName], "members", {}))
            else:
                streamerinfo.members[element.fName] = element

    return streamerinfos, streamerinfosmap, streamerrules

def _ftype2dtype(fType):
    if fType == uproot.const.kBool:
        return "numpy.dtype(numpy.bool_)"
    elif fType == uproot.const.kChar:
        return "numpy.dtype('i1')"
    elif fType in (uproot.const.kUChar, uproot.const.kCharStar):
        return "numpy.dtype('u1')"
    elif fType == uproot.const.kShort:
        return "numpy.dtype('>i2')"
    elif fType == uproot.const.kUShort:
        return "numpy.dtype('>u2')"
    elif fType == uproot.const.kInt:
        return "numpy.dtype('>i4')"
    elif fType in (uproot.const.kBits, uproot.const.kUInt, uproot.const.kCounter):
        return "numpy.dtype('>u4')"
    elif fType == uproot.const.kLong:
        return "numpy.dtype(numpy.long).newbyteorder('>')"
    elif fType == uproot.const.kULong:
        return "numpy.dtype('>u' + repr(numpy.dtype(numpy.long).itemsize))"
    elif fType == uproot.const.kLong64:
        return "numpy.dtype('>i8')"
    elif fType == uproot.const.kULong64:
        return "numpy.dtype('>u8')"
    elif fType in (uproot.const.kFloat, uproot.const.kFloat16):
        return "numpy.dtype('>f4')"
    elif fType in (uproot.const.kDouble, uproot.const.kDouble32):
        return "numpy.dtype('>f8')"
    else:
        return "None"

def _ftype2struct(fType):
    if fType == uproot.const.kBool:
        return "?"
    elif fType == uproot.const.kChar:
        return "b"
    elif fType in (uproot.const.kUChar, uproot.const.kCharStar):
        return "B"
    elif fType == uproot.const.kShort:
        return "h"
    elif fType == uproot.const.kUShort:
        return "H"
    elif fType == uproot.const.kInt:
        return "i"
    elif fType in (uproot.const.kBits, uproot.const.kUInt, uproot.const.kCounter):
        return "I"
    elif fType == uproot.const.kLong:
        return "l"
    elif fType == uproot.const.kULong:
        return "L"
    elif fType == uproot.const.kLong64:
        return "q"
    elif fType == uproot.const.kULong64:
        return "Q"
    elif fType in (uproot.const.kFloat, uproot.const.kFloat16):
        return "f"
    elif fType in (uproot.const.kDouble, uproot.const.kDouble32):
        return "d"
    else:
        raise NotImplementedError(fType)

def _safename(name):
    out = _safename._pattern.sub(lambda bad: "_" + "".join("{0:02x}".format(ord(x)) for x in bad.group(0)) + "_", name.decode("ascii"))
    if keyword.iskeyword(out):
        out = out + "__"
    return out
_safename._pattern = re.compile("[^a-zA-Z0-9]+")

def _raise_notimplemented(streamertype, streamerdict, source, cursor):
    raise NotImplementedError("\n\nUnimplemented streamer type: {0}\n\nmembers: {1}\n\nfile contents:\n\n{2}".format(streamertype, streamerdict, cursor.hexdump(source)))

def _defineclasses(streamerinfos):
    classes = dict(builtin_classes)
    skip = dict(builtin_skip)
    rename = dict((streamerinfo.fName, _safename(streamerinfo.fName)) for streamerinfo in streamerinfos)

    for streamerinfo in streamerinfos:
        if isinstance(streamerinfo, TStreamerInfo) and _safename(streamerinfo.fName) not in classes:
            code = ["    @classmethod",
                    "    def _readinto(cls, self, source, cursor, context):",
                    "        start, cnt, classversion = _startcheck(source, cursor)",
                    "        if classversion != cls.classversion:",
                    "            raise ValueError(\"attempting to read {0} object version {1} with a class generated by streamer version {2}\".format(cls.__name__, classversion, cls.classversion))"]

            fields = []
            bases = []
            formats = {}
            dtypes = {}
            basicnames = []
            basicletters = ""
            for elementi, element in enumerate(streamerinfo.fElements):
                if isinstance(element, TStreamerArtificial):
                    code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, TStreamerBase):
                    code.append("        {0}._readinto(self, source, cursor, context)".format(rename.get(element.fName, element.fName)))
                    bases.append(rename.get(element.fName, element.fName))

                elif isinstance(element, TStreamerBasicPointer):
                    code.append("        cursor.skip(1)")

                    m = re.search(b"\[([^\]]*)\]", element.fTitle)
                    if m is None:
                        raise ValueError("TStreamerBasicPointer fTitle should have a counter name between brackets: {0}".format(repr(element.fTitle)))
                    counter = m.group(1)

                    assert uproot.const.kOffsetP < element.fType < uproot.const.kOffsetP + 20
                    fType = element.fType - uproot.const.kOffsetP

                    dtypename = "_dtype{0}".format(len(dtypes) + 1)
                    dtypes[dtypename] = _ftype2dtype(fType)
                    code.append("        self.{0} = cursor.array(source, self.{1}, {2}.{3})".format(_safename(element.fName), _safename(counter), _safename(streamerinfo.fName), dtypename))
                    fields.append(_safename(element.fName))

                elif isinstance(element, TStreamerBasicType):
                    if element.fArrayLength == 0:
                        basicnames.append("self.{0}".format(_safename(element.fName)))
                        fields.append(_safename(element.fName))
                        basicletters += _ftype2struct(element.fType)

                        if elementi + 1 == len(streamerinfo.fElements) or not isinstance(streamerinfo.fElements[elementi + 1], TStreamerBasicType) or streamerinfo.fElements[elementi + 1].fArrayLength != 0:
                            formatnum = len(formats) + 1
                            formats["_format{0}".format(formatnum)] = "struct.Struct('>{0}')".format(basicletters)

                            if len(basicnames) == 1:
                                code.append("        {0} = cursor.field(source, {1}._format{2})".format(basicnames[0], _safename(streamerinfo.fName), formatnum))
                            else:
                                code.append("        {0} = cursor.fields(source, {1}._format{2})".format(", ".join(basicnames), _safename(streamerinfo.fName), formatnum))

                            basicnames = []
                            basicletters = ""

                    else:
                        dtypename = "_dtype{0}".format(len(dtypes) + 1)
                        dtypes[dtypename] = _ftype2dtype(element.fType)
                        code.append("        self.{0} = cursor.array(source, {1}, {2}.{3})".format(_safename(element.fName), element.fArrayLength, dtypename, _safename(streamerinfo.fName)))
                        fields.append(_safename(element.fName))

                elif isinstance(element, TStreamerLoop):
                    code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, TStreamerObjectAnyPointer):
                    code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, TStreamerObjectPointer):
                    if element.fType == uproot.const.kObjectp:
                        if _safename(streamerinfo.fName) in skip and _safename(element.fName) in skip[_safename(streamerinfo.fName)]:
                            code.append("        Undefined.read(source, cursor, context)")
                        else:
                            code.append("        self.{0} = {1}.read(source, cursor, context)".format(_safename(element.fName), rename.get(element.fTypeName, element.fTypeName.decode("ascii")).rstrip("*")))
                            fields.append(_safename(element.fName))
                    elif element.fType == uproot.const.kObjectP:
                        if _safename(streamerinfo.fName) in skip and _safename(element.fName) in skip[_safename(streamerinfo.fName)]:
                            code.append("        _readobjany(source, cursor, context, asclass=Undefined)")
                        else:
                            code.append("        self.{0} = _readobjany(source, cursor, context)".format(_safename(element.fName)))
                            fields.append(_safename(element.fName))
                    else:
                        code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, TStreamerSTL):
                    code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, TStreamerSTLstring):
                    code.append("        _raise_notimplemented({0}, {1}, source, cursor)".format(repr(element.__class__.__name__), repr(repr(element.__dict__))))

                elif isinstance(element, (TStreamerObject, TStreamerObjectAny, TStreamerString)):
                    if _safename(streamerinfo.fName) in skip and _safename(element.fName) in skip[_safename(streamerinfo.fName)]:
                        code.append("        self.{0} = Undefined.read(source, cursor, context)".format(_safename(element.fName)))
                    else:
                        code.append("        self.{0} = {1}.read(source, cursor, context)".format(_safename(element.fName), rename.get(element.fTypeName, element.fTypeName.decode("ascii"))))
                        fields.append(_safename(element.fName))

                else:
                    raise AssertionError

            code.extend(["        _endcheck(start, cursor, cnt)",
                         "        return self"])

            if len(bases) == 0:
                bases.append("ROOTStreamedObject")
            if _safename(streamerinfo.fName) in methods:
                bases.insert(0, methods[_safename(streamerinfo.fName)].__name__)

            for n, v in sorted(formats.items()):
                code.append("    {0} = {1}".format(n, v))
            for n, v in sorted(dtypes.items()):
                code.append("    {0} = {1}".format(n, v))

            code.insert(0, "    classversion = {0}".format(streamerinfo.fClassVersion))
            if sys.version_info[0] > 2:
                code.insert(0, "    classname = {0}".format(repr(streamerinfo.fName)))
            else:
                code.insert(0, "    classname = b{0}".format(repr(streamerinfo.fName)))
            code.insert(0, "    _fields = [{0}]".format(", ".join(repr(x) for x in fields)))
            code.insert(0, "class {0}({1}):".format(_safename(streamerinfo.fName), ", ".join(bases)))
            classes[_safename(streamerinfo.fName)] = _makeclass(streamerinfo.fName, id(streamerinfo), "\n".join(code), classes)
            classes[_safename(streamerinfo.fName)]._streamerinfo = streamerinfo

    return classes

def _makeclass(classname, id, codestr, classes):
    env = {}
    env.update(globals())
    env.update(classes)
    for methodclass in methods.values():
        env[methodclass.__name__] = methodclass
    exec(compile(codestr, "<generated from TStreamerInfo {0} at 0x{1:012x}>".format(repr(classname), id), "exec"), env)
    out = env[_safename(classname)]
    out._codestr = codestr
    return out

################################################################ built-in ROOT objects for bootstrapping up to streamed classes

class ROOTObject(object):
    # makes __doc__ attribute mutable before Python 3.3
    __metaclass__ = type.__new__(type, "type", (type,), {})

    _copycontext = False

    @classmethod
    def read(cls, source, cursor, context):
        if cls._copycontext:
            context = context.copy()
        out = cls.__new__(cls)
        out = cls._readinto(out, source, cursor, context)
        out._postprocess(source, cursor, context)
        return out

    @classmethod
    def _readinto(cls, self, source, cursor, context):
        raise NotImplementedError

    def _postprocess(self, source, cursor, context):
        pass

    def __repr__(self):
        if hasattr(self, "fName"):
            return "<{0} {1} at 0x{2:012x}>".format(self.__class__.__name__, repr(self.fName), id(self))
        else:
            return "<{0} at 0x{1:012x}>".format(self.__class__.__name__, id(self))

class TKey(ROOTObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start = cursor.index

        self.fNbytes, self.fVersion, self.fObjlen, self.fDatime, self.fKeylen, self.fCycle, self.fSeekKey, self.fSeekPdir = cursor.fields(source, self._format_small)
        if self.fVersion > 1000:
            cursor.index = start
            self.fNbytes, self.fVersion, self.fObjlen, self.fDatime, self.fKeylen, self.fCycle, self.fSeekKey, self.fSeekPdir = cursor.fields(source, self._format_big)

        self.fClassName = cursor.string(source)
        self.fName = cursor.string(source)
        self.fTitle = cursor.string(source)

        # object size != compressed size means it's compressed
        if self.fObjlen != self.fNbytes - self.fKeylen:
            self._source = uproot.source.compressed.CompressedSource(context.compression, source, Cursor(self.fSeekKey + self.fKeylen), self.fNbytes - self.fKeylen, self.fObjlen)
            self._cursor = Cursor(0, origin=-self.fKeylen)

        # otherwise, it's uncompressed
        else:
            self._source = source
            self._cursor = Cursor(self.fSeekKey + self.fKeylen, origin=self.fSeekKey)

        self._context = context
        return self

    _format_small = struct.Struct(">ihiIhhii")
    _format_big   = struct.Struct(">ihiIhhqq")

    def get(self, dismiss=True):
        """Extract the object this key points to.

        Objects are not read or decompressed until this function is explicitly called.
        """

        classname = self.fClassName.decode("ascii")
        try:
            if classname == "TDirectory":
                return ROOTDirectory.read(self._source, self._cursor.copied(), self._context, self)

            elif classname in self._context.classes:
                return self._context.classes[classname].read(self._source, self._cursor.copied(), self._context)

            else:
                out = Undefined.read(self._source, self._cursor.copied(), self._context)
                out.classname = classname
                return out

        finally:
            if dismiss:
                self._source.dismiss()

class TStreamerInfo(ROOTObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        self.fName, _ = _nametitle(source, cursor)
        self.fCheckSum, self.fClassVersion = cursor.fields(source, TStreamerInfo._format)
        self.fElements = _readobjany(source, cursor, context)
        assert isinstance(self.fElements, list)
        _endcheck(start, cursor, cnt)
        return self

    _format = struct.Struct(">Ii")

    def show(self, stream=sys.stdout):
        out = "StreamerInfo for class: {0}, version={1}, checksum=0x{2:08x}\n{3}{4}".format(self.fName, self.fClassVersion, self.fCheckSum, "\n".join("  " + x.show(stream=None) for x in self.fElements), "\n" if len(self.fElements) > 0 else "")
        if stream is None:
            return out
        else:
            stream.write(out)
            stream.write("\n")

class TStreamerElement(ROOTObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):    
        start, cnt, self.version = _startcheck(source, cursor)

        self.fOffset = 0
        # https://github.com/root-project/root/blob/master/core/meta/src/TStreamerElement.cxx#L505
        self.fName, self.fTitle = _nametitle(source, cursor)
        self.fType, self.fSize, self.fArrayLength, self.fArrayDim = cursor.fields(source, TStreamerElement._format1)

        if self.version == 1:
            n = cursor.field(source, TStreamerElement._format2)
            self.fMaxIndex = cursor.array(source, n, ">i4")
        else:
            self.fMaxIndex = cursor.array(source, 5, ">i4")

        self.fTypeName = cursor.string(source)

        if self.fType == 11 and (self.fTypeName == "Bool_t" or self.fTypeName == "bool"):
            self.fType = 18

        if self.version <= 2:
            # FIXME
            # self.fSize = self.fArrayLength * gROOT->GetType(GetTypeName())->Size()
            pass

        self.fXmin, self.fXmax, self.fFactor = 0.0, 0.0, 0.0
        if self.version == 3:
            self.fXmin, self.fXmax, self.fFactor = cursor.fields(source, TStreamerElement._format3)
        if self.version > 3:
            # FIXME
            # if (TestBit(kHasRange)) GetRange(GetTitle(),fXmin,fXmax,fFactor)
            pass

        _endcheck(start, cursor, cnt)
        return self

    _format1 = struct.Struct(">iiii")
    _format2 = struct.Struct(">i")
    _format3 = struct.Struct(">ddd")

    def show(self, stream=sys.stdout):
        out = "{0:15s} {1:15s} offset={2:3d} type={3:2d} {4}".format(self.fName, self.fTypeName, self.fOffset, self.fType, self.fTitle)
        if stream is None:
            return out
        else:
            stream.write(out)
            stream.write("\n")

class TStreamerArtificial(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerArtificial, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerBase(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerBase, self)._readinto(self, source, cursor, context)
        if self.version > 2:
            self.fBaseVersion = cursor.field(source, TStreamerBase._format)
        _endcheck(start, cursor, cnt)
        return self

    _format = struct.Struct(">i")

class TStreamerBasicPointer(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerBasicPointer, self)._readinto(self, source, cursor, context)
        self.fCountVersion = cursor.field(source, TStreamerBasicPointer._format)
        self.fCountName = cursor.string(source)
        self.fCountClass = cursor.string(source)
        _endcheck(start, cursor, cnt)
        return self

    _format = struct.Struct(">i")

class TStreamerBasicType(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerBasicType, self)._readinto(self, source, cursor, context)

        if uproot.const.kOffsetL < self.fType < uproot.const.kOffsetP:
            self.fType -= uproot.const.kOffsetL

        basic = True
        if self.fType in (uproot.const.kBool, uproot.const.kUChar, uproot.const.kChar):
            self.fSize = 1
        elif self.fType in (uproot.const.kUShort, uproot.const.kShort):
            self.fSize = 2
        elif self.fType in (uproot.const.kBits, uproot.const.kUInt, uproot.const.kInt, uproot.const.kCounter):
            self.fSize = 4
        elif self.fType in (uproot.const.kULong, uproot.const.kULong64, uproot.const.kLong, uproot.const.kLong64):
            self.fSize = 8
        elif self.fType in (uproot.const.kFloat, uproot.const.kFloat16):
            self.fSize = 4
        elif self.fType in (uproot.const.kDouble, uproot.const.kDouble32):
            self.fSize = 8
        elif self.fType == uproot.const.kCharStar:
            self.fSize = numpy.dtype(numpy.intp).itemsize
        else:
            basic = False

        if basic and self.fArrayLength > 0:
            self.fSize *= self.fArrayLength

        _endcheck(start, cursor, cnt)
        return self

class TStreamerLoop(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerLoop, self)._readinto(self, source, cursor, context)
        self.fCountVersion = cursor.field(source, TStreamerLoop._format)
        self.fCountName = cursor.string(source)
        self.fCountClass = cursor.string(source)
        _endcheck(start, cursor, cnt)
        return self

    _format = struct.Struct(">i")

class TStreamerObject(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerObject, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerObjectAny(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerObjectAny, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerObjectAnyPointer(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerObjectAnyPointer, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerObjectPointer(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerObjectPointer, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerSTL(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerSTL, self)._readinto(self, source, cursor, context)

        self.fSTLtype, self.fCtype = cursor.fields(source, TStreamerSTL._format)

        if self.fSTLtype == uproot.const.kSTLmultimap or self.fSTLtype == uproot.const.kSTLset:
            if self.fTypeName.startswith("std::set") or self.fTypeName.startswith("set"):
                self.fSTLtype = uproot.const.kSTLset
            elif self.fTypeName.startswith("std::multimap") or self.fTypeName.startswith("multimap"):
                self.fSTLtype = uproot.const.kSTLmultimap

        _endcheck(start, cursor, cnt)
        return self

    _format = struct.Struct(">ii")

class TStreamerSTLstring(TStreamerSTL):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerSTLstring, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

class TStreamerString(TStreamerElement):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        super(TStreamerString, self)._readinto(self, source, cursor, context)
        _endcheck(start, cursor, cnt)
        return self

################################################################ streamed classes (with some overrides)

class ROOTStreamedObject(ROOTObject):
    pass

class TObject(ROOTStreamedObject):
    _fields = []

    @classmethod
    def _readinto(cls, self, source, cursor, context):
        _skiptobj(source, cursor)
        return self

class TString(bytes, ROOTStreamedObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        return TString(cursor.string(source))

class TNamed(TObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        TObject._readinto(self, source, cursor, context)
        self.fName = cursor.string(source)
        self.fTitle = cursor.string(source)
        _endcheck(start, cursor, cnt)
        return self

class TObjArray(list, ROOTStreamedObject):
    @classmethod
    def read(cls, source, cursor, context, asclass=None):
        if cls._copycontext:
            context = context.copy()
        out = cls.__new__(cls)
        out = cls._readinto(out, source, cursor, context, asclass=asclass)
        out._postprocess(source, cursor, context)
        return out

    @classmethod
    def _readinto(cls, self, source, cursor, context, asclass=None):
        start, cnt, self.version = _startcheck(source, cursor)
        _skiptobj(source, cursor)
        name = cursor.string(source)
        size, low = cursor.fields(source, struct.Struct(">ii"))
        self.extend([_readobjany(source, cursor, context, asclass=asclass) for i in range(size)])
        _endcheck(start, cursor, cnt)
        return self

class TObjString(str, ROOTStreamedObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        _skiptobj(source, cursor)
        string = cursor.string(source)
        _endcheck(start, cursor, cnt)
        return TObjString(string)

class TList(list, ROOTStreamedObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        start, cnt, self.version = _startcheck(source, cursor)
        _skiptobj(source, cursor)
        name = cursor.string(source)
        size = cursor.field(source, struct.Struct(">i"))
        for i in range(size):
            self.append(_readobjany(source, cursor, context))
            n = cursor.field(source, TList._format_n)  # ignore option
            cursor.bytes(source, n)                    # 
        _endcheck(start, cursor, cnt)
        return self
    _format_n = struct.Struct(">B")

class TArray(list, ROOTStreamedObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        length = cursor.field(source, TArray._format)
        self.extend(cursor.array(source, length, self._dtype))
        return self
    _format = struct.Struct(">i")

class TArrayC(TArray):
    _dtype = numpy.dtype(">i1")

class TArrayS(TArray):
    _dtype = numpy.dtype(">i2")

class TArrayI(TArray):
    _dtype = numpy.dtype(">i4")

class TArrayL(TArray):
    _dtype = numpy.dtype(numpy.int_).newbyteorder(">")

class TArrayL64(TArray):
    _dtype = numpy.dtype(">i8")

class TArrayF(TArray):
    _dtype = numpy.dtype(">f4")

class TArrayD(TArray):
    _dtype = numpy.dtype(">f8")

class Undefined(ROOTStreamedObject):
    @classmethod
    def _readinto(cls, self, source, cursor, context):
        self._cursor = cursor.copied()
        start, cnt, self.version = _startcheck(source, cursor)
        cursor.skip(cnt - 6)
        _endcheck(start, cursor, cnt)
        return self

    def __repr__(self):
        if hasattr(self, "classname"):
            return "<{0} (no class named {1}) at 0x{2:012x}>".format(self.__class__.__name__, repr(self.classname), id(self))
        else:
            return "<{0} at 0x{1:012x}>".format(self.__class__.__name__, id(self))

builtin_classes = {"TObject":    TObject,
                   "TNamed":     TNamed,
                   "TString":    TString,
                   "TList":      TList,
                   "TObjArray":  TObjArray,
                   "TObjString": TObjString,
                   "TArrayC":    TArrayC,
                   "TArrayS":    TArrayS,
                   "TArrayI":    TArrayI,
                   "TArrayL":    TArrayL,
                   "TArrayL64":  TArrayL64,
                   "TArrayF":    TArrayF,
                   "TArrayD":    TArrayD}

builtin_skip =    {"TBranch":    ["fBaskets"]}
