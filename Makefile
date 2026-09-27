# Building with make on Linux or macOS.  python build.py does the same on any system, including Windows.
#   make              the xqchess engine: engine/xqchess
#   make -j4 fairy    Fairy-Stockfish: engine/fairy-xqchess
#   make all          both
#   make check        perft counts of the xqchess engine, to compare with the published values
#   make web          start the web page (python3 webplay.py)
#   make clean        remove the built engines

CXX ?= g++
CXXFLAGS ?= -O2 -std=c++17 -pthread -Wall -Wno-misleading-indentation

FAIRY_SRCS := $(wildcard fairy/src/*.cpp fairy/src/nnue/*.cpp fairy/src/nnue/features/*.cpp fairy/src/syzygy/*.cpp)
FAIRY_HDRS := $(wildcard fairy/src/*.h fairy/src/nnue/*.h fairy/src/nnue/*/*.h fairy/src/syzygy/*.h)
FAIRY_OBJS := $(patsubst fairy/src/%.cpp,build/fairy/%.o,$(FAIRY_SRCS))
FAIRY_FLAGS ?= -O3 -std=c++17 -DNDEBUG -DNNUE_EMBEDDING_OFF -DUSE_POPCNT -DIS_64BIT -pthread
ifeq ($(shell uname -m),x86_64)
  FAIRY_FLAGS += -mpopcnt
endif
ifeq ($(shell uname -s),Linux)
  FAIRY_FLAGS += -DUSE_PTHREADS
endif

engine/xqchess: engine/xqchess.cpp
	$(CXX) $(CXXFLAGS) -o $@ $<

fairy: engine/fairy-xqchess

engine/fairy-xqchess: $(FAIRY_OBJS)
	$(CXX) $(FAIRY_FLAGS) -o $@ $(FAIRY_OBJS)

build/fairy/%.o: fairy/src/%.cpp $(FAIRY_HDRS)
	@mkdir -p $(dir $@)
	$(CXX) $(FAIRY_FLAGS) -c -o $@ $<

all: engine/xqchess engine/fairy-xqchess

check: engine/xqchess
	@printf 'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1\nperft 5\nposition fen r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1\nperft 4\nsetoption Snipers true\nposition fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1\nperft 5\nquit\n' | ./engine/xqchess
	@echo "expected: 4865609 and 4085603 (chess from the start, and Kiwipete), then 4898029 (snipers chess)"

web:
	python3 webplay.py

clean:
	rm -rf engine/xqchess engine/fairy-xqchess build

.PHONY: all fairy check web clean
