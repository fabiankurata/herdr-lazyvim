return dofile(debug.getinfo(1, "S").source:sub(2):gsub("/lua/herdr_review.lua$", "/nvim/lua/herdr_review.lua"))
