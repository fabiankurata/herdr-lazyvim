return dofile(debug.getinfo(1, "S").source:sub(2):gsub("/lua/herdr_feedback/completion.lua$", "/nvim/lua/herdr_feedback/completion.lua"))
