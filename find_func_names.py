# This script will attempt save and load function names between different cisco IOS images. As the functions can change between
#   versions, this can be a hard process to automate.  This script makes the assumption that the strings used in each function
#   will remain constant.  Failing unique strings, it assumes that the functions that are called from a function are constant.
#   This is not a perfect means of finding functions, but can be useful for transfering a large number of functions from one 
#   image to another quickly.
# This script follows this algorithm:  
# 1) Locate the strings that the function references.  If there are unique ones, then we can use these to find the function
# 2) Locate the strings that near calls to the desired function.  Then, find the prev/next call and we've found our function. 
#      Since this method might get confused by instruction reordering, we attempt to verify by using strings inside the found 
#      function (which may or may not be unique)
# 3) Locate unique strings that the functions our function calls.  Find those functions, then look at their references and see
#      if we can pick out our desired function from that.  As this increases the number of functions, it will take much longer
import sys, collections, pickle, operator

#Heuristics:
MAX_UNIQUE_STRINGS = 2
MAX_UNIQUE_CALLING_STRINGS = 5
MAX_CALLED_FUNCTIONS = 10

#Per Architecture Variables (MIPS values are below)
ADDR_WIDTH = 4
JMP_CALL_PREFIXES = ["j", "b"]

class FunctionInfo(object):

  def __init__(self, name, address):
    self.name = name
    self.start = address
    self.end = GetFunctionAttr(address, FUNCATTR_END)
    self.strings = []
    self.calling_strings = []
    self.called_funcs = []

  def __str__(self):
    return "%s(0x%08x-0x%08x)" % (self.name, self.start, self.end)

  def cs(self, string): #clean string
    return string.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")

  def long_str(self):
    ret = "%s\n  Strings:\n" % str(self)
    for string in self.strings:
      ret += '    %s: "%s"\n' % ("Unique" if string[1] else "Not Unique", self.cs(string[0]))
    ret += "  Calling Strings:\n"
    for string in self.calling_strings:
      ret += '    %s %s: "%s"\n' % ("Unique" if string[1] else "Not Unique", "Forward" if string[2] < 0 else "Backwards", 
                self.cs(string[0]))
    ret += "  Called Funcs:\n"
    for func_info in self.called_funcs:
      ret += "    %s\n" % (func_info)
    return ret

  def add_string(self, string, unique):
    self.strings.append((string, unique))

  def expand_strings(self, string_infos):
    for string_info in string_infos:
      self.add_string(string_info[0], string_info[1])

  def add_calling_string(self, string, unique, direction):
    self.calling_strings.append((string, unique, direction))

  def set_call_info(self, call_info):
    self.called_funcs = call_info

  def has_enough(self):
    if len([s for s in self.strings if s[1]]) >= MAX_UNIQUE_STRINGS: # Stop after we have enough unique strings
      return True
    if len([s for s in self.calling_strings if s[1]]) >= MAX_UNIQUE_CALLING_STRINGS: # Stop after unique calling strings
      return True
    return False

class CiscoFunctionFinder(object):

  def __init__(self):
    self.strings = Strings() #save a reference for speed

    #Sort the data and cache it for speed
    self.string_addrs = []
    self.string_cnts = collections.defaultdict(int)
    self.string_name_to_addrs = collections.defaultdict(list)
    for string in self.strings:
      string_content = str(string)
      self.string_addrs.append(string.ea)
      self.string_name_to_addrs[string_content].append(string.ea)
      self.string_cnts[string_content] += 1

    #Cached data
    self.full_func_defs = {}
    self.string_func_defs = {}

    #Functions labels that are default prefixes autogenerated by IDA.  We shouldn't save these names
    self.bad_prefixes = [ "sub_", "nullsub_", "def_" ]
    #Function labels that are autogenerated by IDA.  We shouldn't save these names
    self.bad_names = [ "start" ]

  def count_generator(self, generator):
    cnt = 0
    for foo in generator:
      cnt += 1
    return cnt

  def string_is_unique(self, string_address):
    string = GetString(string_address)
    count = self.count_generator(XrefsTo(string_address)) #get the number of references to this string
    return (count == 1) and self.string_cnts[string] == 1 #make sure there's only 1 reference and no duplicate strings

  def get_strings_from_function(self, func):
    func_strings = []
    for address in range(func.start, func.end, ADDR_WIDTH):  #Find strings in the function
      for x in XrefsFrom(address):
        if x.to in self.string_addrs:
          string = GetString(x.to)
          func_strings.append((string, self.string_is_unique(x.to)))
    return func_strings

  def generate_call_info(self, func):
    called_funcs = {}

    print "  Generating call info for %s" % func.name
    for address in range(func.start, func.end, ADDR_WIDTH):
      if idaapi.is_call_insn(address):
        func_address = self.get_call_destination(address)
        if func_address != None and func_address not in called_funcs:
          called_funcs[func_address] = GetFunctionName(func_address)
          print "    %s calls %s (0x%08x) at 0x%08x" % (func.name, called_funcs[func_address], func_address, address)
      
    called_func_infos = []
    for address, name in called_funcs.items():
      called_func = FunctionInfo(name, address)
      called_func = self.generate_function_def(called_func, False) #Don't do a second round of function call info, we don't want endless recursion
      called_func_infos.append(called_func)

    return called_func_infos

  def generate_function_def(self, func, generate_called_funcs = True):
    #See if we have a cached copy of this function definition
    if generate_called_funcs and func.name in self.full_func_defs:
      return self.full_func_defs[func.name]
    elif not generate_called_funcs and func.name in self.string_func_defs:
      return self.string_func_defs[func.name]

    func.expand_strings(self.get_strings_from_function(func)) #Get strings in the function
    for func_reference in XrefsTo(func.start): #Get calling strings
      address = func_reference.frm
      if idaapi.is_call_insn(address): #only deal with calls to the function
        func_start = GetFunctionAttr(address, FUNCATTR_START)
        func_end = GetFunctionAttr(address, FUNCATTR_END)

        #Search forward for a string
        string_address = self.search_for_string_before_call(address, func_end, ADDR_WIDTH)
        if string_address != None:
          func.add_calling_string(GetString(string_address), self.string_is_unique(string_address), ADDR_WIDTH)

        #Search backwards for a string
        string_address = self.search_for_string_before_call(address, func_start, -ADDR_WIDTH)
        if string_address != None:
          func.add_calling_string(GetString(string_address), self.string_is_unique(string_address), -ADDR_WIDTH)

      if func.has_enough():
        break

    if generate_called_funcs and not func.has_enough(): #Ugh, couldn't find function with strings or calling strings. Instead find the functions it calls
      print "  String search for %s failed.  Using function call dependence instead" % str(func) 
      func_calls_info = self.generate_call_info(func)
      func.set_call_info(func_calls_info)

    if generate_called_funcs: #Do some caching to speed things up
      self.full_func_defs[func.name] = func
    else:
      self.string_func_defs[func.name] = func

    return func

  def generate_function_defs(self):
    functions = []
    for address in Functions(): #Find the functions with custom names
      name = GetFunctionName(address)
      custom_named = True
      for bad in self.bad_prefixes:
        if name.startswith(bad):
          custom_named = False
      for bad in self.bad_names:
        if name == bad:
          custom_named = False
      if custom_named:
        functions.append(FunctionInfo(name, address))

    print "Creating function definitions (%d functions)" % len(functions)
    for func in functions:
      func = self.generate_function_def(func)
      print "Generated function defintion %s" % func.long_str()
    return functions

  def search_for_string_before_call(self, address, end_address, direction, call_only = False):
    current = address + direction
    mnem = GetMnem(current)
    while (((direction > 0 and current < end_address) or (direction < 0 and current > end_address)) and 
      ((call_only and not idaapi.is_call_insn(current)) or (not call_only and (len(mnem) == 0 or mnem[0] not in JMP_CALL_PREFIXES)))):
      for x in XrefsFrom(current):
        if x.to in self.string_addrs:
          return x.to
      current += direction
      mnem = GetMnem(current)
    return None

  def find_function_from_string(self, func):
    for string, unique in func.strings:
      if unique and len(self.string_name_to_addrs[string]) == 1:
        string_address = self.string_name_to_addrs[string][0]
        if self.count_generator(XrefsTo(string_address)) == 1:
          reference_address = next(XrefsTo(string_address)).frm
          function_address = GetFunctionAttr(reference_address, FUNCATTR_START)
          print "    Found %s (0x%08x) from unique string (0x%08x) \"%s\"" % (func.name, function_address, string_address, 
                  func.cs(string))
          return function_address
    return None

  def get_call_destination(self, address):
    for x in XrefsFrom(address):
      if GetFunctionAttr(x.to, FUNCATTR_START) != GetFunctionAttr(address, FUNCATTR_START): # filter out the reference to the next instruction
        return x.to                    # for some reason IDA thinks calls reference the next instruction
    return None

  def search_for_call(self, address, end_address, direction):
    current = address + direction
    while ((direction > 0 and current < end_address) or (direction < 0 and current > end_address)) and not idaapi.is_call_insn(current):
      current += direction
    if (direction > 0 and current < end_address) or (direction < 0 and current > end_address): #we found a call
      return self.get_call_destination(current)
    return None

  def find_function_from_calling_string(self, func):
    function_addresses = collections.defaultdict(int)
    for string, unique, direction in func.calling_strings:
      direction = -direction #Flip direction to find it
      if unique and len(self.string_name_to_addrs[string]) == 1:
        string_address = self.string_name_to_addrs[string][0]
        if self.count_generator(XrefsTo(string_address)) == 1:
          reference_address = next(XrefsTo(string_address)).frm
          end_address = GetFunctionAttr(reference_address, FUNCATTR_END)
          if direction < 0:
            end_address = GetFunctionAttr(reference_address, FUNCATTR_START)
          function_address = self.search_for_call(reference_address, end_address, direction)
          if function_address != None:
            print "    Found %s (0x%08x) from unique calling string (0x%08x) \"%s\"" % (func.name, function_address, 
                    string_address, func.cs(string))
            function_addresses[function_address] += 1

    addresses = function_addresses.keys()
    if len(addresses) == 1: #if there's only one address found, we got it (horay!)
      return addresses[0]
    elif len(addresses) == 0: # couldn't find anything (boo!)
      return None

    #if not, we need to do some verifying
    for string, unique in func.strings: #First try to find a string in the function (even if it's not unique)
      string_address = self.string_name_to_addrs[string][0]
      for reference in XrefsTo(string_address):
        reference_address = reference.frm
        function_address = GetFunctionAttr(reference_address, FUNCATTR_START)
        for address in addresses:
          if address == function_address:
            return address

    #else, just go with which ever one had more calling functions point to it (this might make it find printf)
    return max(function_address.iteritems(), key=operator.itemgetter(1))

  def find_from_called_functions(self, func):
    called_func_addresses = [] 
    for called_func in func.called_funcs:
      called_func_address = self.find_function(called_func)
      if called_func_address != None:
        called_func_addresses.append(called_func_address)
    
    possible_addresses = collections.defaultdict(int)
    for called_func_address in called_func_addresses:
      already_found = []
      for xref in XrefsTo(called_func_address):
        calling_function_address = GetFunctionAttr(xref.frm, FUNCATTR_START)
        if calling_function_address not in already_found: #multiple calls to the same function don't count twice
          possible_addresses[calling_function_address] += 1
          already_found.append(calling_function_address)

    if len(possible_addresses) > 0:
      return max(possible_addresses.iteritems(), key=operator.itemgetter(1))[0]
    return None

  def find_function(self, func):
    function_address = self.find_function_from_string(func)
    if function_address == None:
      function_address = self.find_function_from_calling_string(func)
    if function_address == None:
      function_address = self.find_from_called_functions(func)
    return function_address
  
  def write_function_labels(self, functions):
    print "Loading function labels (%d functions)" % len(functions)
    for func in functions:
      print "  Searching for %s" % func.name
      function_address = self.find_function(func)
      if function_address == None:
        print "    Couldn't find function '%s'" % func.name
      else:
        print "    Setting function at 0x%08x to the name '%s'" % (function_address, func.name)
        #MakeName(function_address, func.name)

  def save_to_file(self, filename):
    functions = self.generate_function_defs()
    dump_file = open(filename, "w")
    pickle.dump(functions, dump_file)
    dump_file.close()

  def load_from_file(self, filename):
    dump_file = open(filename, "r")
    functions = pickle.load(dump_file)
    dump_file.close()

    self.write_function_labels(functions)

def save_function_names(*args):
  filename = AskFile(1, "*.dump", "Please select a functions dump file")
  if filename != None:
    finder = CiscoFunctionFinder()
    finder.save_to_file(filename)

def open_function_dump(*args):
  filename = AskFile(0, "*.dump", "Please select a functions dump file")
  if filename != None:
    finder = CiscoFunctionFinder()
    finder.load_from_file(filename)

try: #Test to see if it's already defined
  cisco_function_finder_menu_items
except:
  cisco_function_finder_menu_items = {}

def add_menu_item(label, func):
  if label in cisco_function_finder_menu_items:
    idaapi.del_menu_item(cisco_function_finder_menu_items[label])
    cisco_function_finder_menu_items.pop(label)

  menu_item = idaapi.add_menu_item("File/", label, "", 0, func, None)
  if menu_item != None:
    cisco_function_finder_menu_items[label] = menu_item

add_menu_item("Save function dump file", save_function_names) #TODO find a better place to put these menu items
add_menu_item("Load function dump file", open_function_dump)

